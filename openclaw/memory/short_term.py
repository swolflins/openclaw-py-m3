"""短期记忆:SQLite turns + scope + metadata + JSON 备份。

为了向后兼容,保留原 `MemoryStore` 名字作为别名。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openclaw.llm.base import ChatMessage


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
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, check_same_thread=False)

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
        with self._lock, self._conn() as conn:
            conn.executemany(
                "INSERT INTO turns(scope, role, content, metadata, ts) VALUES(?,?,?,?,?)",
                [
                    (scope, "user", user, meta_json, ts),
                    (scope, "assistant", assistant, meta_json, ts),
                ],
            )
            conn.commit()
        self._backup(scope)

    def recent(self, scope: str, k: int = 20) -> list[ChatMessage]:
        with self._lock, self._conn() as conn:
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
        return [ChatMessage(role=r, content=c) for r, c in rows]

    def clear(self, scope: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM turns WHERE scope = ?", (scope,))
            conn.commit()
        p = self.dir / f"{scope.replace(':', '_')}.json"
        if p.exists():
            p.unlink()

    def all_scopes(self) -> list[str]:
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT DISTINCT scope FROM turns").fetchall()
        return [r[0] for r in rows]

    def _backup(self, scope: str) -> None:
        rows = self.recent(scope, k=200)
        p = self.dir / f"{scope.replace(':', '_')}.json"
        try:
            p.write_text(
                json.dumps([m.to_dict() for m in rows], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass


# 兼容旧名
MemoryStore = ShortTermStore
