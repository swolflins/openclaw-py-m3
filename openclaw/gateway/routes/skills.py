"""/v1/skills 列出 / 重载。"""
from __future__ import annotations


from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from openclaw.gateway.deps import get_deps
from openclaw.gateway.util import to_jsonable

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("")
async def list_skills() -> dict:
    """列出现有 skills。"""
    deps = get_deps()
    if deps.config is None:
        return {"skills": [], "count": 0, "directories": []}
    try:
        from openclaw.core.skills import load_skills
        sreg = load_skills(*getattr(deps.config.skills, "directories", []))
    except Exception as e:
        raise HTTPException(500, f"load_skills error: {e}") from e
    skills = []
    for s in sreg.skills():
        skills.append(to_jsonable(s))
    return {
        "count": len(skills),
        "directories": list(getattr(deps.config.skills, "directories", [])),
        "skills": skills,
    }


class SkillReloadRequest(BaseModel):
    directories: list[str] | None = None
    """为空时用 config.skills.directories。"""


@router.post("/reload")
async def reload_skills(req: SkillReloadRequest) -> dict:
    """重载 skills 目录。返回新注入的 prompt(可拼到 system_prompt)。"""
    deps = get_deps()
    if deps.config is None and req.directories is None:
        raise HTTPException(400, "no config and no directories provided")
    dirs = req.directories or list(getattr(deps.config.skills, "directories", []))
    try:
        from openclaw.core.skills import load_skills
        sreg = load_skills(*dirs, registry=getattr(deps.agent_loop, "tools", None) if deps.agent_loop else None)
    except Exception as e:
        raise HTTPException(500, f"load error: {e}") from e
    return {
        "directories": dirs,
        "count": len(sreg.skills()),
        "prompt_injections": sreg.prompt_injections(),
    }
