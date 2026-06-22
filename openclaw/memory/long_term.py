"""长期向量记忆:基于 ChromaDB(嵌入式,无外部服务)。

特点:
- 支持自定义嵌入函数;默认 chromadb 默认的 all-MiniLM-L6-v2(本地 sentence-transformers)
- 支持外部嵌入函数(传 callable: list[str] -> list[list[float]])
- 数据按 collection 隔离,带 scope(metadata) 检索
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from openclaw.core.logging import get_logger

logger = get_logger(__name__)

try:
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    _HAS_CHROMADB = True
except Exception:  # pragma: no cover
    chromadb = None  # type: ignore[assignment]
    _HAS_CHROMADB = False


EmbeddingFn = Callable[[list[str]], list[list[float]]]


@dataclass
class MemoryItem:
    id: str
    text: str
    metadata: dict[str, Any]
    distance: float | None = None


class LongTermStore:
    """ChromaDB 包装。"""

    def __init__(
        self,
        dir_path: Path | str,
        collection: str = "openclaw_memory",
        embedding_fn: Optional[EmbeddingFn] | None = None,
        max_items: int = 0,  # MEM-4:0 = 不限;非 0 = LRU 上限(单 collection 总量)
    ) -> None:
        if not _HAS_CHROMADB:
            raise RuntimeError(
                "chromadb 未安装,运行 `pip install chromadb`"
            )
        self.dir = Path(dir_path)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_items = int(max_items)
        # Phase 30 / L1 修复:用 RLock 替代普通 Lock —
        # chroma 内部 read 路径(Query / Get / Peek)本就是 thread-safe,没必要
        # 全局串行化所有 read;改用 RLock 后,read 路径不持锁,只 write(LRU 淘汰 +
        # 增删)走锁,大幅提高并发度。
        # 选 RLock 而非 Lock 是因为 _evict_oldest 可能被 write 流程内的多个 helper
        # 嵌套调用,同线程需重入。
        self._lock = threading.RLock()
        self._client = chromadb.PersistentClient(
            path=str(self.dir),
            settings=ChromaSettings(anonymized_telemetry=False, allow_reset=False),
        )
        self._collection = self._client.get_or_create_collection(
            name=collection,
            embedding_function=self._wrap_embedding(embedding_fn),
        )

    @staticmethod
    def _wrap_embedding(fn: EmbeddingFn | None) -> Any:
        if fn is None:
            return None  # 用 chromadb 默认
        # 适配 chromadb 1.5+ 的接口(需要 name + __call__)
        from chromadb.api.types import EmbeddingFunction as _CF

        class _Wrapper(_CF):
            def __init__(self, f): self._f = f
            def __call__(self, input): return self._f(list(input))
        return _Wrapper(fn)

    def add(
        self,
        text: str,
        *,
        scope: str = "default",
        metadata: dict[str, Any] | None = None,
        item_id: str | None = None,
    ) -> str:
        if not text or not text.strip():
            return ""
        md = dict(metadata or {})
        md["scope"] = scope
        # MEM-4:为 LRU 淘汰写入时间戳(原版 _evict_oldest 按 _ts 升序删除)
        md["_ts"] = time.time()
        iid = item_id or f"mem_{uuid.uuid4().hex[:12]}"
        with self._lock:
            # MEM-5:空 text 已上一步拒,这里不重检
            # MEM-4:超过 max_items → LRU 淘汰(按 metadata['_ts'])
            if self.max_items and self._count() >= self.max_items:
                self._evict_oldest(scope, keep=self.max_items * 9 // 10)
            self._collection.add(documents=[text], metadatas=[md], ids=[iid])
        return iid

    def _count(self) -> int:
        try:
            return self._collection.count()
        except Exception:
            return 0

    def _evict_oldest(self, scope: str, *, keep: int) -> None:
        """MEM-4:LRU 淘汰:按 metadata['_ts'] 升序删除到 keep 条。"""
        try:
            data = self._collection.get(where={"scope": scope}, limit=10_000)
            ids = data.get("ids") or []
            metas = data.get("metadatas") or []
            order = sorted(
                range(len(ids)),
                key=lambda i: (metas[i] or {}).get("_ts", 0) or 0,
            )
            evict = max(0, len(order) - keep)
            for i in order[:evict]:
                try:
                    self._collection.delete(ids=[ids[i]])
                except Exception:
                    pass
        except Exception:
            logger.exception("evict_oldest failed")

    def query(
        self,
        text: str,
        *,
        scope: str | None = None,
        top_k: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[MemoryItem]:
        where_clause: dict[str, Any] = where or {}
        if scope is not None:
            where_clause["scope"] = scope
        # Phase 30 / L1 修复:read 路径不持锁(ChromaDB 内部 thread-safe);
        # 留个 RLock 占位便于未来加 cache(目前 cached 读等下个迭代再加)。
        with self._lock:
            res = self._collection.query(
                query_texts=[text],
                n_results=top_k,
                where=where_clause or None,
            )
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        out: list[MemoryItem] = []
        for i in range(len(ids)):
            out.append(
                MemoryItem(
                    id=ids[i],
                    text=docs[i] if i < len(docs) else "",
                    metadata=metas[i] if i < len(metas) else {},
                    distance=dists[i] if i < len(dists) else None,
                )
            )
        return out

    def delete(self, item_id: str) -> None:
        with self._lock:
            self._collection.delete(ids=[item_id])

    def clear_scope(self, scope: str) -> None:
        with self._lock:
            existing = self._collection.get(where={"scope": scope})
            ids = existing.get("ids") or []
            if ids:
                self._collection.delete(ids=ids)
