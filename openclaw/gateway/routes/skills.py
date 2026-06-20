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
    """重载 skills 目录。返回新注入的 prompt(可拼到 system_prompt)。

    安全(SEC-6):
    - 只允许 reload config.skills.directories 中已配置的目录
    - 即便传了 req.directories,也只能从中挑,**不接受**新目录
      (避免通过 reload 端点 + write_file 实现 RCE)
    """
    deps = get_deps()
    allowed = list(getattr(deps.config.skills, "directories", [])) if deps.config else []
    if not allowed:
        raise HTTPException(400, "no allowed skill directories in config")

    if req.directories is None:
        dirs = [str(d) for d in allowed]
    else:
        # 只挑白名单中的,其余忽略(允许 str/Path 混合)
        allowed_str = {str(d) for d in allowed}
        dirs = [d for d in req.directories if d in allowed_str or d in allowed]
        if not dirs:
            raise HTTPException(
                403,
                f"no requested directory is in allowlist (allowed={sorted(allowed_str)})",
            )
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
