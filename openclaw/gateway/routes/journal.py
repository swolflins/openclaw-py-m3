"""Journal HTTP 路由(Phase 22)— 把 AgentJournal 暴露到 gateway。

**SEC**:路径越界 / 错误返回 503/500 而非 200 假装 OK。
**rate-limit**:不在 /v1/chat prefix,不走限流(只读不写)。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from openclaw.core.logging import get_logger
from openclaw.gateway.deps import get_deps

logger = get_logger(__name__)

router = APIRouter(prefix="/journal", tags=["journal"])


def _journal() -> Any:
    """取 AgentJournal 实例;无则 503。"""
    deps = get_deps()
    if deps.journal is None:
        raise HTTPException(
            status_code=503,
            detail="AgentJournal not configured (set OPENCLAW_JOURNAL_DIR or attach deps.journal)",
        )
    return deps.journal


@router.get("/entries")
async def list_entries(
    days: int = Query(7, ge=1, le=90, description="回看天数"),
) -> dict:
    """列出最近 N 天的 journal entry 路径 + 摘要元数据。"""
    j = _journal()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    files = j.list_entries(since=since)
    out: list[dict] = []
    for fp in files:
        rel = str(fp.relative_to(j.root))
        # 简单解析:从 md 文件头抓时间 / 迭代 / 标签
        text = fp.read_text(encoding="utf-8")
        meta = _parse_entry_meta(text)
        meta["path"] = rel
        out.append(meta)
    return {"entries": out, "count": len(out)}


@router.get("/entries/read")
async def read_entry(path: str = Query(..., description="相对 path,如 2026-06-20/sess_xxx.md")) -> dict:
    """读一个 entry 的完整内容(限制在 journal root 内,防越界)。"""
    j = _journal()
    full = (j.root / path).resolve()
    root_resolved = Path(j.root).resolve()
    if not str(full).startswith(str(root_resolved) + "/") and full != root_resolved:
        raise HTTPException(status_code=400, detail="path escapes journal root")
    if not full.exists():
        raise HTTPException(status_code=404, detail="entry not found")
    return {"path": path, "content": full.read_text(encoding="utf-8")}


@router.post("/weekly")
async def generate_weekly() -> dict:
    """触发周报生成,返回文件路径 + 摘要。"""
    j = _journal()
    p = j.weekly_report()
    rel = str(p.relative_to(j.root))
    return {"weekly_report": rel, "content": p.read_text(encoding="utf-8")[:2000]}


@router.get("/soul-proposals")
async def read_soul_proposals() -> dict:
    """读 _soul_proposals.md 全文(dry-run,不会改 SOUL)。

    无 journal 时返回 200 + 空 proposals(其他端点 503,这条更友好,只是读静态文件)。
    """
    deps = get_deps()
    if deps.journal is None:
        return {"proposals": "", "exists": False, "note": "journal not configured"}
    j = deps.journal
    p = j.root / "_soul_proposals.md"
    if not p.exists():
        return {"proposals": "", "exists": False}
    return {"proposals": p.read_text(encoding="utf-8"), "exists": True}


def _parse_entry_meta(text: str) -> dict[str, Any]:
    """从 journal md 头解析元数据(轻量正则,容错)。"""
    import re
    out: dict[str, Any] = {
        "session_id": "",
        "timestamp": "",
        "iterations": 0,
        "tags": [],
    }
    sid_m = re.search(r"Session `([^`]+)`", text)
    if sid_m:
        out["session_id"] = sid_m.group(1)
    ts_m = re.search(r"\*\*时间\*\*:\s*([\dT:\-\+Z]+)", text)
    if ts_m:
        out["timestamp"] = ts_m.group(1)
    iter_m = re.search(r"\*\*迭代\*\*:\s*(\d+)", text)
    if iter_m:
        out["iterations"] = int(iter_m.group(1))
    tag_m = re.search(r"\*\*标签\*\*:\s*([^_\n]+)", text)
    if tag_m:
        out["tags"] = [t.strip() for t in tag_m.group(1).split(",") if t.strip() and t.strip() != "_(待 reflect)_"]
    return out
