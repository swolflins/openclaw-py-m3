"""Phase 4 测试:工具全量。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from openclaw.tools.builtin import register_builtin_tools
from openclaw.tools.registry import (
    ToolCategory,
    ToolPermission,
    ToolRegistry,
)


# ---------------- registry ----------------

def test_registry_categories_and_permissions():
    reg = ToolRegistry()

    @reg.tool(category=ToolCategory.FS, permission=ToolPermission.READ)
    def read_x(path: str) -> str:
        return path

    @reg.tool(category=ToolCategory.SHELL, permission=ToolPermission.EXEC)
    def run_x(cmd: str) -> str:
        return cmd

    fs_tools = reg.list_tools(category=ToolCategory.FS)
    assert len(fs_tools) == 1
    assert fs_tools[0].name == "read_x"

    safe_only = reg.list_tools(max_permission=ToolPermission.WRITE)
    assert {t.name for t in safe_only} == {"read_x"}


def test_registry_approver_blocks_dangerous():
    reg = ToolRegistry()
    called = {"n": 0}

    async def approver(name, args):
        called["n"] += 1
        return False  # 全部拒绝

    @reg.tool(category=ToolCategory.SHELL, permission=ToolPermission.EXEC)
    def boom(cmd: str) -> str:
        return cmd

    reg.set_approver(approver)
    with pytest.raises(PermissionError):
        asyncio.run(reg.call("boom", {"cmd": "ls"}))


def test_registry_specs_filtered():
    reg = ToolRegistry()
    register_builtin_tools(reg, include=["calculator", "echo"], fs_root=".")
    specs = reg.specs()
    assert {s.name for s in specs} == {"calculator", "echo"}


# ---------------- shell ----------------

def test_shell_exec_runs_command(tmp_path: Path):
    reg = ToolRegistry()
    register_builtin_tools(reg, shell_default_cwd=str(tmp_path), fs_root=str(tmp_path))
    out = asyncio.run(reg.call("shell_exec", {"command": "echo hello", "timeout": 5}))
    assert "hello" in out
    assert "[exit=0]" in out


def test_shell_exec_respects_allowlist(tmp_path: Path):
    reg = ToolRegistry()
    register_builtin_tools(
        reg, shell_default_cwd=str(tmp_path), fs_root=str(tmp_path),
        shell_allowed=["echo"],
    )
    with pytest.raises(PermissionError):
        asyncio.run(reg.call("shell_exec", {"command": "ls /", "timeout": 5}))


def test_shell_exec_rejects_metachar(tmp_path: Path):
    reg = ToolRegistry()
    register_builtin_tools(
        reg, shell_default_cwd=str(tmp_path), fs_root=str(tmp_path),
        shell_allowed=["ls"],  # 显式开 allow-list 才检查 metachar
    )
    with pytest.raises(PermissionError):
        asyncio.run(reg.call("shell_exec", {"command": "ls && rm -rf /", "timeout": 5}))


# ---------------- fs ----------------

def test_fs_read_write_list(tmp_path: Path):
    reg = ToolRegistry()
    register_builtin_tools(reg, fs_root=str(tmp_path))
    asyncio.run(reg.call("write_file", {"path": "a.txt", "content": "hi"}))
    text = asyncio.run(reg.call("read_file", {"path": "a.txt"}))
    assert "hi" in text
    listing = asyncio.run(reg.call("list_dir", {"path": "."}))
    assert "a.txt" in listing


def test_fs_blocks_path_escape(tmp_path: Path):
    reg = ToolRegistry()
    register_builtin_tools(reg, fs_root=str(tmp_path))
    with pytest.raises(PermissionError):
        asyncio.run(reg.call("read_file", {"path": "../escaped.txt"}))


def test_fs_refuses_overwrite_without_flag(tmp_path: Path):
    reg = ToolRegistry()
    register_builtin_tools(reg, fs_root=str(tmp_path))
    asyncio.run(reg.call("write_file", {"path": "a.txt", "content": "x"}))
    out = asyncio.run(reg.call("write_file", {"path": "a.txt", "content": "y"}))
    assert "[error]" in out
    # overwrite=true 成功
    out2 = asyncio.run(reg.call("write_file", {"path": "a.txt", "content": "y", "overwrite": True}))
    assert "wrote" in out2


def test_fs_search(tmp_path: Path):
    reg = ToolRegistry()
    register_builtin_tools(reg, fs_root=str(tmp_path))
    asyncio.run(reg.call("write_file", {"path": "src/main.py", "content": "def foo(): pass\n"}))
    asyncio.run(reg.call("write_file", {"path": "src/util.py", "content": "x = 1\n"}))
    found = asyncio.run(reg.call("search_files", {"path": ".", "pattern": "**/*.py"}))
    assert "main.py" in found and "util.py" in found


# ---------------- http ----------------

def test_http_get_blocks_disallowed_host():
    reg = ToolRegistry()
    register_builtin_tools(reg, http_allowed_hosts=["example.com"])
    with pytest.raises(PermissionError):
        asyncio.run(reg.call("http_get", {"url": "https://evil.com/"}))
    # 允许的 host 不在测试环境跑(避免依赖外网)


# ---------------- datetime ----------------

def test_datetime_tools():
    reg = ToolRegistry()
    register_builtin_tools(reg, include=["get_current_time", "format_time", "parse_time", "timezone_convert", "date_diff"], fs_root=".")
    now = asyncio.run(reg.call("get_current_time", {"tz": "UTC"}))
    assert "UTC" in now or "GMT" in now
    iso = asyncio.run(reg.call("parse_time", {"s": "2026-06-19 10:00:00", "fmt": "%Y-%m-%d %H:%M:%S"}))
    assert "2026-06-19" in iso
    diff = asyncio.run(reg.call("date_diff", {"iso_a": "2026-06-19T12:00:00", "iso_b": "2026-06-19T10:00:00", "unit": "hours"}))
    assert diff.strip() == "2.0"


# ---------------- cron ----------------

def test_cron_add_and_list_and_remove():
    reg = ToolRegistry()
    register_builtin_tools(reg, include=["cron_add", "cron_list", "cron_remove"], fs_root=".")
    jid_line = asyncio.run(reg.call("cron_add", {"every_seconds": 60, "payload": "ping"}))
    assert "job_" in jid_line
    # 提取 job_id
    job_id = next(tok for tok in jid_line.split() if tok.startswith("job_"))
    listed = asyncio.run(reg.call("cron_list", {}))
    assert job_id in listed
    removed = asyncio.run(reg.call("cron_remove", {"job_id": job_id}))
    assert "removed" in removed


def test_cron_add_rejects_empty():
    reg = ToolRegistry()
    register_builtin_tools(reg, include=["cron_add"], fs_root=".")
    out = asyncio.run(reg.call("cron_add", {}))
    assert "[error]" in out


# ---------------- docker (跳过如果未装) ----------------

def test_docker_tools_optional():
    reg = ToolRegistry()
    register_builtin_tools(reg, include=["docker_list_images"], fs_root=".")
    # 不会抛(可能为空)
    out = asyncio.run(reg.call("docker_list_images", {}))
    assert isinstance(out, str)
