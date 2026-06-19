"""工作区索引:跟踪 agent 看到的文件 + 摘要,后续给 Agent 提示用。

仅维护元数据 + 摘要,文件本身可被文件工具读写。
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
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

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, check_same_thread=False)

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
