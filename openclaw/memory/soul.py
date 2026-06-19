"""SOUL / AGENTS / 知识文档加载。

与 TS 版 OpenClaw 兼容:
- 默认搜索路径(按顺序合并,先存在的优先):
    1. ./SOUL.md
    2. ./AGENTS.md
    3. ./.openclaw/SOUL.md
    4. ~/.openclaw/SOUL.md
- 知识目录: ./knowledge/**.md(每个文件作为单独 system 块)
- 段落级 frontmatter 支持(YAML),用 `---` 包裹:
    ---
    scope: user:123
    ---
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

from openclaw.core.logging import get_logger

logger = get_logger(__name__)


DEFAULT_PATHS = [
    Path("./SOUL.md"),
    Path("./AGENTS.md"),
    Path("./.openclaw/SOUL.md"),
    Path("~/.openclaw/SOUL.md").expanduser(),
]


@dataclass
class SoulDoc:
    path: Path
    content: str
    scope: str = "global"  # global / session / user
    metadata: dict = field(default_factory=dict)


class SoulLoader:
    """加载并缓存 SOUL 文档。"""

    def __init__(
        self,
        paths: Iterable[Path] | None = None,
        knowledge_dir: Path | str | None = None,
    ) -> None:
        self.paths: list[Path] = list(paths) if paths else list(DEFAULT_PATHS)
        self.knowledge_dir = Path(knowledge_dir) if knowledge_dir else None
        self._cache: list[SoulDoc] | None = None

    def load(self, use_cache: bool = True) -> list[SoulDoc]:
        if use_cache and self._cache is not None:
            return self._cache

        docs: list[SoulDoc] = []
        for p in self.paths:
            if p.exists():
                docs.append(self._parse(p))

        if self.knowledge_dir and self.knowledge_dir.exists():
            for p in sorted(self.knowledge_dir.rglob("*.md")):
                docs.append(self._parse(p))

        self._cache = docs
        logger.info("soul_loaded", count=len(docs), paths=[str(d.path) for d in docs])
        return docs

    def reload(self) -> list[SoulDoc]:
        self._cache = None
        return self.load(use_cache=False)

    def render_system_prompt(self, base: str = "") -> str:
        """把所有 SOUL 文档拼成一个 system prompt(带路径头注释)。"""
        docs = self.load()
        parts: list[str] = []
        if base:
            parts.append(base)
        for d in docs:
            header = f"\n\n<!-- soul: {d.path} | scope={d.scope} -->\n"
            parts.append(header + d.content.strip())
        return "".join(parts).strip()

    @staticmethod
    def _parse(path: Path) -> SoulDoc:
        text = path.read_text(encoding="utf-8")
        scope = "global"
        meta: dict = {}
        # 简单 frontmatter 解析:仅取文件首段 ---
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
        if m:
            try:
                meta = yaml.safe_load(m.group(1)) or {}
                scope = str(meta.get("scope", "global"))
                text = m.group(2)
            except yaml.YAMLError:
                pass
        return SoulDoc(path=path, content=text, scope=scope, metadata=meta)
