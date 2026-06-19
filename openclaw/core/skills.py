"""Skill 体系(Phase 6):类似 Claude Skills 的可插拔技能包。

一个 Skill 是一个目录,结构:
    my_skill/
    ├── SKILL.md           # 必填,描述 + 触发条件(yaml front matter)
    └── skill.py           # 可选,定义 register(skill_api) 函数

SKILL.md 例子:
    ---
    name: weather
    version: 0.1.0
    description: 查天气
    triggers: [天气, weather, 下雨]
    requires_tools: [http_get]   # 提示,实际由 skill 自己用
    ---

    # Weather Skill
    当用户问天气时使用本 skill,优先用 http_get 调天气 API。

skill.py 例子:
    from openclaw.core.skills import SkillAPI

    def register(api: SkillAPI) -> None:
        @api.tool(name="weather_query", description="查天气", category=ToolCategory.UTILITY)
        def weather_query(city: str) -> str:
            ...
        api.inject_prompt("今天用户可能问天气,优先用 weather_query 工具。")
"""
from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from openclaw.core.logging import get_logger
from openclaw.tools.registry import ToolCategory, ToolPermission, ToolRegistry

logger = get_logger(__name__)


# ---------------- Skill 元数据 ----------------

@dataclass
class Skill:
    name: str
    version: str = "0.0.0"
    description: str = ""
    triggers: list[str] = field(default_factory=list)
    requires_tools: list[str] = field(default_factory=list)
    # 加载后填
    path: Optional[Path] = None
    prompt_injections: list[str] = field(default_factory=list)


def _parse_front_matter(md: str) -> tuple[dict[str, Any], str]:
    """极简 YAML front matter 解析(只支持 key: value / key: [a, b])。"""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", md, re.S)
    if not m:
        return {}, md
    raw, body = m.group(1), m.group(2)
    meta: dict[str, Any] = {}
    current_list: Optional[str] = None
    for line in raw.splitlines():
        line = line.rstrip()
        if not line:
            current_list = None
            continue
        if line.startswith("  - ") and current_list:
            meta[current_list].append(line[4:].strip().strip("'\""))
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                inner = v[1:-1].strip()
                meta[k] = [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]
                current_list = None
            elif v == "" or v.lower() == "|":
                meta[k] = []
                current_list = k
            else:
                meta[k] = v.strip("'\"")
                current_list = None
    return meta, body


def _load_skill_md(path: Path) -> Optional[Skill]:
    raw = path.read_text(encoding="utf-8")
    meta, _ = _parse_front_matter(raw)
    if not meta.get("name"):
        logger.warning("skill_no_name", path=str(path))
        return None
    return Skill(
        name=str(meta["name"]),
        version=str(meta.get("version", "0.0.0")),
        description=str(meta.get("description", "")),
        triggers=list(meta.get("triggers", [])),
        requires_tools=list(meta.get("requires_tools", [])),
        path=path.parent,
    )


# ---------------- Skill API(给 skill.py 用) ----------------

class SkillAPI:
    """skill.py 的 register(api) 拿到的对象,用于向 host 注入。"""

    def __init__(self, skill: Skill, registry: ToolRegistry) -> None:
        self.skill = skill
        self.registry = registry
        self._prompt: list[str] = []

    def tool(
        self,
        *,
        name: Optional[str] = None,
        description: str = "",
        category: ToolCategory = ToolCategory.CUSTOM,
        permission: ToolPermission = ToolPermission.SAFE,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """装饰器:把一个函数注册为 skill 自带工具。"""
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            full_name = name or fn.__name__
            doc = description or (fn.__doc__ or "").strip()
            self.registry.register(
                fn,
                name=full_name,
                description=doc,
                category=category,
                permission=permission,
            )
            return fn
        return deco

    def inject_prompt(self, text: str) -> None:
        """追加一段到 skill 加载时拼接到 system_prompt 的文本。"""
        self.skill.prompt_injections.append(text)

    def log(self, msg: str, **kw: Any) -> None:
        logger.info(f"skill[{self.skill.name}]: {msg}", **kw)


# ---------------- Skill Registry / Loader ----------------

class SkillRegistry:
    """已加载的 skill 集合,负责拼装 prompt_injections。"""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
        self._tool_registry = ToolRegistry()

    def add(self, skill: Skill) -> None:
        self._skills[skill.name] = skill
        # 加载 skill.py
        if skill.path is None:
            return
        sp = skill.path / "skill.py"
        if not sp.exists():
            return
        try:
            mod_name = f"_openclaw_skill_{skill.name}"
            spec = importlib.util.spec_from_file_location(mod_name, sp)
            if not spec or not spec.loader:
                return
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            reg = getattr(mod, "register", None)
            if reg is None:
                return
            api = SkillAPI(skill, self._tool_registry)
            reg(api)
        except Exception:
            logger.exception("skill_module_load_failed", skill=skill.name, path=str(sp))

    def skills(self) -> list[Skill]:
        return list(self._skills.values())

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def tool_registry(self) -> ToolRegistry:
        return self._tool_registry

    def prompt_injections(self) -> str:
        """把所有 skill 的 prompt_injections 拼起来,准备塞到 system_prompt。"""
        lines: list[str] = []
        for s in self.skills():
            if s.prompt_injections:
                lines.append(f"## Skill: {s.name} (v{s.version})")
                lines.extend(s.prompt_injections)
        return "\n".join(lines)


def load_skills(
    *directories: Path | str,
    registry: Optional[ToolRegistry] = None,
) -> SkillRegistry:
    """扫描若干目录,加载所有含 SKILL.md 的子目录。"""
    sreg = SkillRegistry()
    if registry is not None:
        sreg._tool_registry = registry  # 共享同一个 tool registry
    for d in directories:
        d = Path(d)
        if not d.exists():
            continue
        for sub in sorted(d.iterdir()):
            if not sub.is_dir():
                continue
            md = sub / "SKILL.md"
            if not md.exists():
                continue
            sk = _load_skill_md(md)
            if sk is None:
                continue
            sreg.add(sk)
            logger.info("skill_loaded", name=sk.name, path=str(sub))
    return sreg
