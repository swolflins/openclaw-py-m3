"""Journal HTTP 路由(Phase 22)— 把 AgentJournal 暴露到 gateway。

**SEC**:路径越界 / 错误返回 503/500 而非 200 假装 OK。
**rate-limit**:不在 /v1/chat prefix,不走限流(只读不写)。
"""
from __future__ import annotations

import asyncio
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
    """列出最近 N 天的 journal entry 路径 + 摘要元数据。

    Phase 27 / M7 修复:把 ``fp.read_text`` 同步 I/O 走 ``asyncio.to_thread``,
    防止大目录 / 慢磁盘场景下阻塞 event loop。
    """
    j = _journal()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    files = j.list_entries(since=since)
    out: list[dict] = []

    def _parse_one(fp: Path) -> dict:
        rel = str(fp.relative_to(j.root))
        text = fp.read_text(encoding="utf-8")
        meta = _parse_entry_meta(text)
        meta["path"] = rel
        return meta

    # 并发读(用 to_thread 释放 event loop);单文件 parse 仍顺序,IO 走线程
    parsed = await asyncio.gather(*(asyncio.to_thread(_parse_one, fp) for fp in files))
    out = list(parsed)
    return {"entries": out, "count": len(out)}


@router.get("/entries/read")
async def read_entry(path: str = Query(..., description="相对 path,如 2026-06-20/sess_xxx.md")) -> dict:
    """读一个 entry 的完整内容(限制在 journal root 内,防越界)。

    Phase 27 / C5 修复:用 ``Path.is_relative_to`` 替代 ``str.startswith`` 字符串拼接,
    避免相对 root 时 ``str(full).startswith(str(root)+"/")`` 误判边界(case:
    root="foo", full="foobar" 会被旧实现判成"越界"或"未越界"取决于边界 char)。

    还加了三层加固:
    1. 拒绝绝对路径(``path`` 以 ``/`` 起头会让 ``Path.__truediv__`` 替换 root)
    2. 拒绝包含 NUL 字符
    3. ``is_relative_to``(Python 3.9+)语义明确,无需手写边界拼接
    """
    j = _journal()
    if not path:
        raise HTTPException(status_code=400, detail="path must be non-empty")
    if "\x00" in path:
        raise HTTPException(status_code=400, detail="path contains NUL character")
    if path.startswith("/") or path.startswith("\\"):
        raise HTTPException(
            status_code=400,
            detail="absolute path not allowed (use relative path under journal root)",
        )
    full = await asyncio.to_thread(lambda: (j.root / path).resolve())
    root_resolved = await asyncio.to_thread(lambda: Path(j.root).resolve())
    # Python 3.9+ 的 is_relative_to 比 str.startswith 更稳健:
    # - 自动处理 root 末尾是否带分隔符
    # - 跨平台(Windows 大小写不敏感)由 Path 自身负责
    if not (full == root_resolved or full.is_relative_to(root_resolved)):
        raise HTTPException(status_code=400, detail="path escapes journal root")
    if not full.exists():
        raise HTTPException(status_code=404, detail="entry not found")
    # Phase 27 / M7:read_text 走 to_thread
    content = await asyncio.to_thread(full.read_text, encoding="utf-8")
    return {"path": path, "content": content}


@router.post("/weekly")
async def generate_weekly() -> dict:
    """触发周报生成,返回文件路径 + 摘要。

    Phase 27 follow-up / M16 修复:``j.weekly_report()`` 本身是同步(内部
    跑 ``fp.read_text`` 拼字符串),在 threadpool / 慢磁盘 / 大量 entry 场景
    下会阻塞 event loop。Phase 27 M7 已经把 ``read_text`` 单独包了
    ``asyncio.to_thread``,但 weekly_report() 整体同步(读 N 个 entry + 解析
    + 写 weekly_<x>.md)仍然阻塞。**修法**:整个调用包 ``asyncio.to_thread``,
    让 weekly 报告在后台线程跑,event loop 立刻可以接下一个请求。
    """
    j = _journal()
    p = await asyncio.to_thread(j.weekly_report)
    rel = str(p.relative_to(j.root))
    # Phase 27 / M7:read_text 走 to_thread
    text = await asyncio.to_thread(p.read_text, encoding="utf-8")
    return {"weekly_report": rel, "content": text[:2000]}


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
    # Phase 27 / M7:read_text 走 to_thread
    text = await asyncio.to_thread(p.read_text, encoding="utf-8")
    return {"proposals": text, "exists": True}


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
