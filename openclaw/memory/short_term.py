"""短期记忆:SQLite turns + scope + metadata + JSON 备份。

为了向后兼容,保留原 `MemoryStore` 名字作为别名。

安全要点(MEM-1/2/3):
- scope → 备份文件名:用 sha256(scope) hex 避免 ../ 与不可见字符
- SQLite 连接:每次显式 .close(),设 WAL + busy_timeout 防多进程锁
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openclaw.llm.base import ChatMessage


_SCOPE_NAME_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,200}$")


def _safe_scope_name(scope: str) -> str:
    """scope → 8 字节 hex(防路径穿越 / 不可见字符)。"""
    if not _SCOPE_NAME_RE.match(scope):
        # 任何异常字符 → 强制 hash
        return hashlib.sha256(scope.encode("utf-8", errors="replace")).hexdigest()[:16]
    # 即便通过白名单,也走 hash(避免 session_id 里的 ':' 让文件目录结构变怪)
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT    NOT NULL,           -- 形如 'session:abc' 或 'user:u1'
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    metadata   TEXT    NOT NULL DEFAULT '{}',
    ts         REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_scope ON turns(scope, id);
"""


@dataclass
class Turn:
    role: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


class ShortTermStore:
    """短期记忆存储。"""

    def __init__(self, dir_path: Path | str) -> None:
        self.dir = Path(dir_path)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "memory.sqlite"
        # Phase 25 review follow-up:用 RLock(可重入)而非 Lock。
        # ``append()`` 写完会在同一线程内调 ``_backup()`` → ``recent()``,
        # 而 ``recent()`` 会再次 ``with self._lock``。``threading.Lock``
        # 不可重入,一旦 ``_backup`` 在持锁上下文里被调用就会死锁/阻塞;
        # RLock 允许同线程重复获取,消除该死锁风险(且对当前"锁外调
        # _backup"的写法零行为变化)。
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        # MEM-3:WAL 模式 + busy_timeout → 多进程/多 worker 写不锁死
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        # MEM-1 修复:返回的连接调用方必须 close
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        # 每个新连接也设置一次(连接级,不是 DB 级)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def append(
        self,
        scope: str,
        user: str,
        assistant: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """追加一轮对话(同时记到指定 scope)。"""
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        ts = time.time()
        # MEM-1:显式 close + try/finally。
        # Phase 25 review follow-up:把 ``_backup`` 移入锁内 —— 写 + 备份做成
        # 原子快照(避免"刚 commit 另一线程又 append"导致备份串了的竞态)。
        # ``_backup`` → ``recent()`` 会再次 ``with self._lock``;因为这里用的是
        # RLock(可重入),同线程二次获取不会死锁。若改回不可重入的 Lock,
        # 这里会立刻死锁 —— RLock 是必需的。
        with self._lock:
            conn = self._conn()
            try:
                conn.executemany(
                    "INSERT INTO turns(scope, role, content, metadata, ts) VALUES(?,?,?,?,?)",
                    [
                        (scope, "user", user, meta_json, ts),
                        (scope, "assistant", assistant, meta_json, ts),
                    ],
                )
                conn.commit()
            finally:
                conn.close()
            self._backup(scope)

    def recent(self, scope: str, k: int = 20) -> list[ChatMessage]:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    """
                    SELECT role, content FROM (
                      SELECT role, content, id FROM turns
                      WHERE scope = ?
                      ORDER BY id DESC
                      LIMIT ?
                    ) ORDER BY id ASC
                    """,
                    (scope, k),
                ).fetchall()
            finally:
                conn.close()
        return [ChatMessage(role=r, content=c) for r, c in rows]

    def clear(self, scope: str) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM turns WHERE scope = ?", (scope,))
                conn.commit()
            finally:
                conn.close()
        # MEM-2 修复:scope 名 hash,不再有 ../ 风险
        p = self.dir / f"{_safe_scope_name(scope)}.json"
        if p.exists():
            p.unlink()

    def all_scopes(self) -> list[str]:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute("SELECT DISTINCT scope FROM turns").fetchall()
            finally:
                conn.close()
        return [r[0] for r in rows]

    def _backup(self, scope: str) -> None:
        rows = self.recent(scope, k=200)
        # MEM-2 修复
        p = self.dir / f"{_safe_scope_name(scope)}.json"
        try:
            p.write_text(
                json.dumps([m.to_dict() for m in rows], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass


# 兼容旧名
MemoryStore = ShortTermStore
