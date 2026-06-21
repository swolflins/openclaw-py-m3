"""工作区索引:跟踪 agent 看到的文件 + 摘要,后续给 Agent 提示用。

仅维护元数据 + 摘要,文件本身可被文件工具读写。
"""
from __future__ import annotations

import contextlib
import hashlib
import sqlite3
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path

from openclaw.core.logging import get_logger

logger = get_logger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    mtime       REAL    NOT NULL,
    sha256      TEXT    NOT NULL,
    summary     TEXT    NOT NULL DEFAULT '',
    last_seen   REAL    NOT NULL
);
"""


@dataclass
class FileEntry:
    path: str
    size: int
    mtime: float
    sha256: str
    summary: str = ""


class WorkspaceIndex:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._lock, self._conn() as c:
            c.executescript(_SCHEMA)
            c.commit()

    @contextlib.contextmanager
    def _conn(self):
        """打开一个新 SQLite 连接,with 退出时**自动 close**。

        **Phase 25 / b10 修复**:
        每次 ``sqlite3.connect`` 都开新 fd,如果不显式 ``close()`` 会泄漏。
        现在的统一做法:用 ``contextlib.closing`` 包装,确保 ``with`` 块
        退出时一定 close;同时挂 ``weakref.finalize`` 兜底,即使
        调用方绕过 context manager,实例 GC 时也会自动 close。
        """
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        # 兜底:实例被 GC 时,如果连接还活着,就关掉它
        try:
            weakref.finalize(self, _close_silently, conn)
        except TypeError:  # pragma: no cover - self 不支持 weakref
            pass
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:  # pragma: no cover
                pass

    def upsert(self, file_path: Path, *, summary: str = "") -> FileEntry:
        p = file_path.resolve()
        data = p.read_bytes()
        sha = hashlib.sha256(data).hexdigest()[:16]
        stat = p.stat()
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO files(path,size,mtime,sha256,summary,last_seen) "
                "VALUES(?,?,?,?,?,?)",
                (str(p), stat.st_size, stat.st_mtime, sha, summary, now),
            )
            c.commit()
        return FileEntry(path=str(p), size=stat.st_size, mtime=stat.st_mtime, sha256=sha, summary=summary)

    def get(self, file_path: Path | str) -> FileEntry | None:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT path,size,mtime,sha256,summary FROM files WHERE path=?",
                (str(Path(file_path).resolve()),),
            ).fetchone()
        if not row:
            return None
        return FileEntry(*row)

    def list_recent(self, k: int = 50) -> list[FileEntry]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT path,size,mtime,sha256,summary FROM files ORDER BY last_seen DESC LIMIT ?",
                (k,),
            ).fetchall()
        return [FileEntry(*r) for r in rows]


def _close_silently(conn: sqlite3.Connection) -> None:
    """``weakref.finalize`` 回调:实例 GC 时安全 close 残留连接。

    用模块级函数(非闭包)以保证 weakref 可序列化/可哈希。
    """
    try:
        conn.close()
    except Exception:  # pragma: no cover
        pass
