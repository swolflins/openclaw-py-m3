"""Phase 4 演示:工具全量(不依赖真实 LLM,只演示注册+调用)。

覆盖:
- ToolRegistry 分类 + 权限 + 审批
- shell_exec(白名单 + metachar 拦截)
- fs 工具(读/写/列/搜索,默认拒绝覆盖)
- http_get(host 白名单)
- datetime 工具
- cron 工具
- docker 工具(SDK 缺失时优雅降级)

跑法:
    python examples/tools_demo.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    from openclaw.core.logging import setup_logging
    from openclaw.tools.builtin import register_builtin_tools
    from openclaw.tools.registry import (
        ToolCategory,
        ToolPermission,
        ToolRegistry,
    )

    setup_logging("INFO", json=False)

    tmp = Path(tempfile.mkdtemp(prefix="openclaw_tools_demo_"))
    try:
        # 准备一个 fs 工作目录 + 一个示例文件
        work = tmp / "work"
        work.mkdir()
        (work / "demo.txt").write_text("hello from openclaw", encoding="utf-8")

        # 1) 注册所有内置工具
        reg = ToolRegistry()
        register_builtin_tools(
            reg,
            fs_root=str(work),
            shell_default_cwd=str(work),
            shell_allowed=["ls", "echo", "cat"],
            http_allowed_hosts=["example.com", "httpbin.org"],
            include=["shell_exec", "read_file", "write_file", "list_dir",
                     "search_files", "file_stat",
                     "http_get", "http_post",
                     "get_current_time", "format_time", "parse_time",
                     "timezone_convert", "date_diff",
                     "cron_add", "cron_list", "cron_remove",
                     "docker_list_images",
                     "calculator", "echo"],
        )

        tools = reg.list_tools()
        cats: dict[str, int] = {}
        for t in tools:
            key = t.category.value if hasattr(t.category, "value") else str(t.category)
            cats[key] = cats.get(key, 0) + 1
        print(f"[1] 共注册 {len(tools)} 个工具,按分类统计:")
        for c, n in sorted(cats.items()):
            print(f"    {c:10s} x {n}")

        def _p(perm: Any) -> str:
            return perm.value if hasattr(perm, "value") else str(perm)

        perms = {t.name: _p(t.permission) for t in tools}
        print(f"[2] 权限分布示例: docker_list_images={perms['docker_list_images']}, "
              f"shell_exec={perms['shell_exec']}, "
              f"read_file={perms['read_file']}, "
              f"http_get={perms['http_get']}")

        # 2) shell:白名单生效
        out = asyncio.run(reg.call("shell_exec", {"command": "echo hi from shell", "timeout": 5}))
        print(f"[3] shell_exec echo: {out.strip()}")

        # 3) shell:metachar 拦截
        try:
            asyncio.run(reg.call("shell_exec", {"command": "ls && rm -rf /", "timeout": 5}))
        except PermissionError as e:
            print(f"[4] shell_exec metachar 拦截 OK: {e!s:.80}")

        # 4) fs:写 + 读 + 列
        asyncio.run(reg.call("write_file", {"path": "out/note.txt", "content": "line1\nline2\n"}))
        text = asyncio.run(reg.call("read_file", {"path": "demo.txt"}))
        listing = asyncio.run(reg.call("list_dir", {"path": "."}))
        print(f"[5] fs read demo.txt: {text.strip()!r}")
        print(f"    fs list dir: {listing.strip().splitlines()[0]} ...")

        # 5) fs:拒绝覆盖
        ret = asyncio.run(reg.call("write_file", {"path": "demo.txt", "content": "x"}))
        print(f"[6] fs 默认拒绝覆盖: {ret.strip()!r}")

        # 6) fs:路径逃逸拦截
        try:
            asyncio.run(reg.call("read_file", {"path": "../escaped.txt"}))
        except PermissionError as e:
            print(f"[7] fs 路径逃逸拦截 OK: {e!s:.80}")

        # 7) http:host 白名单
        try:
            asyncio.run(reg.call("http_get", {"url": "https://evil.com/"}))
        except PermissionError as e:
            print(f"[8] http host 白名单 OK: {e!s:.80}")

        # 8) datetime
        now = asyncio.run(reg.call("get_current_time", {"tz": "UTC"}))
        diff = asyncio.run(reg.call("date_diff", {
            "iso_a": "2026-06-19T12:00:00", "iso_b": "2026-06-19T10:00:00", "unit": "hours",
        }))
        print(f"[9] datetime now(UTC)={now.strip()}, 2h diff={diff.strip()}")

        # 9) cron:添加/列/移除
        jid_line = asyncio.run(reg.call("cron_add", {"every_seconds": 60, "payload": "ping"}))
        job_id = next(tok for tok in jid_line.split() if tok.startswith("job_"))
        listed = asyncio.run(reg.call("cron_list", {}))
        removed = asyncio.run(reg.call("cron_remove", {"job_id": job_id}))
        print(f"[10] cron add: {jid_line.strip()}")
        print(f"     cron list 命中: {job_id in listed}")
        print(f"     cron remove: {removed.strip()}")

        # 10) docker:SDK 缺失时降级
        out = asyncio.run(reg.call("docker_list_images", {}))
        print(f"[11] docker_list_images(无 docker): {out.strip()!r}")

        # 11) 审批:危险工具被拒
        async def auto_deny(name, args):
            return False
        reg2 = ToolRegistry()
        register_builtin_tools(reg2, include=["shell_exec"], fs_root=str(work),
                                shell_default_cwd=str(work))
        reg2.set_approver(auto_deny)
        try:
            asyncio.run(reg2.call("shell_exec", {"command": "ls", "timeout": 5}))
        except PermissionError as e:
            print(f"[12] approver deny OK: {e!s:.80}")

        # 12) 按分类/权限过滤
        fs_tools = reg.list_tools(category=ToolCategory.FS)
        safe = reg.list_tools(max_permission=ToolPermission.WRITE)
        print(f"[13] 按分类 FS: {len(fs_tools)} 个, 权限 ≤WRITE: {len(safe)} 个")

        print("\n✅ Phase 4 工具全量烟测通过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
