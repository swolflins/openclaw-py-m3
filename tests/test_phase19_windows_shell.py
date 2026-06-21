"""Phase 19:Windows 兼容 — shlex 模式自动切换 + shell_exec 接受 argv list。

覆盖:
- _split_command:POSIX 上保持 posix=True(行为不变)
- _split_command:Windows 路径不被吃反斜杠(关键 bug 修复)
- _split_command:POSIX 引号语义保留原样
- shell_exec:接受 list[str] 形式,绕过 shlex 路径解析
- shell_exec:list 模式也要走白名单
- shell_exec:list 模式拒绝空 list
- shell_exec:list 模式拒绝不在白名单的命令
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest


# C1 修复后,requires_approval 工具在无 approver 时 fail-closed。
# 测试中需要一个 always-approve 的 approver。
def _set_test_approver(reg: ToolRegistry) -> None:
    async def _ok(name, args):
        return True
    reg.set_approver(_ok)


# ─────────────── _split_command ───────────────
def test_split_posix_default():
    """非 Windows 走 POSIX(保持原行为)。"""
    if sys.platform == "win32":
        pytest.skip("POSIX 行为只在非 Windows 上测")
    from openclaw.tools.builtin.shell import _split_command

    # 简单分词
    assert _split_command("ls -la /tmp") == ["ls", "-la", "/tmp"]
    # POSIX 模式:反斜杠是 escape
    assert _split_command("echo a\\ b") == ["echo", "a b"]


def test_split_windows_path_safe():
    """Windows 路径不会被吃反斜杠(关键 bug 修复)。

    跨平台都过:在任何平台上,这条命令解析后都应包含 'C:' / 'foo' / 'bar' 三个 token。
    """
    from openclaw.tools.builtin.shell import _split_command

    cmd = 'dir "C:\\Users\\foo\\bar"'
    out = _split_command(cmd)
    # 至少一个 token 包含完整路径
    assert any("C:" in t and "foo" in t and "bar" in t for t in out), (
        f"Windows 路径被吃: {out}"
    )


def test_split_quoted_posix_unchanged():
    """POSIX 模式:引号包裹的字符串保留原样。"""
    if sys.platform == "win32":
        pytest.skip("POSIX 行为只在非 Windows 上测")
    from openclaw.tools.builtin.shell import _split_command

    # 单引号
    assert _split_command("echo 'hello world'") == ["echo", "hello world"]
    # 双引号
    assert _split_command('echo "hello world"') == ["echo", "hello world"]


def test_split_empty_returns_empty():
    """空字符串返回空列表。"""
    from openclaw.tools.builtin.shell import _split_command

    assert _split_command("") == []
    assert _split_command("   ") == []


# ─────────────── shell_exec:接受 list 模式 ───────────────
def _get_shell_tool(tmp_path: Path):
    """helper:注册 shell 工具并返回 tool 函数。"""
    from openclaw.tools.builtin import shell as shell_mod
    from openclaw.tools.registry import ToolRegistry

    reg = ToolRegistry()
    shell_mod.register_shell_tools(
        reg,
        default_cwd=str(tmp_path),
        allowed=["echo", "ls", "cat"],
    )
    return reg


def test_shell_exec_accepts_argv_list(tmp_path: Path):
    """shell_exec 直接接受 list[str] 形式(Windows 推荐,绕过 shlex 路径解析)。"""
    reg = _get_shell_tool(tmp_path)
    _set_test_approver(reg)  # C1: shell_exec requires approval
    out = asyncio.run(
        reg.call("shell_exec", {"command": ["echo", "hello", "world"], "timeout": 5})
    )
    assert "hello world" in out
    assert "[exit=0]" in out


def test_shell_exec_argv_list_runs_cwd(tmp_path: Path):
    """list 模式也遵守 cwd 参数。"""
    reg = _get_shell_tool(tmp_path)
    _set_test_approver(reg)  # C1: shell_exec requires approval
    (tmp_path / "test.txt").write_text("from argv list", encoding="utf-8")
    out = asyncio.run(
        reg.call("shell_exec", {"command": ["ls"], "cwd": str(tmp_path), "timeout": 5})
    )
    assert "test.txt" in out


def test_shell_exec_argv_list_enforces_allowlist(tmp_path: Path):
    """list 模式:首词不在白名单应被拒。"""
    reg = _get_shell_tool(tmp_path)
    _set_test_approver(reg)  # C1: shell_exec requires approval
    with pytest.raises(PermissionError, match="not in allow-list"):
        asyncio.run(
            reg.call("shell_exec", {"command": ["rm", "-rf", "/"], "timeout": 5})
        )


def test_shell_exec_empty_list_rejected(tmp_path: Path):
    """list 模式:空列表应被拒。"""
    reg = _get_shell_tool(tmp_path)
    _set_test_approver(reg)  # C1: shell_exec requires approval
    with pytest.raises(PermissionError, match="empty command"):
        asyncio.run(reg.call("shell_exec", {"command": [], "timeout": 5}))


def test_shell_exec_string_mode_still_works(tmp_path: Path):
    """向后兼容:str 模式仍能跑。"""
    reg = _get_shell_tool(tmp_path)
    _set_test_approver(reg)  # C1: shell_exec requires approval
    out = asyncio.run(
        reg.call("shell_exec", {"command": "echo hi", "timeout": 5})
    )
    assert "hi" in out
    assert "[exit=0]" in out


def test_shell_exec_string_mode_still_blocks_metachar(tmp_path: Path):
    """向后兼容:str 模式仍拦截 metachar。"""
    reg = _get_shell_tool(tmp_path)
    _set_test_approver(reg)  # C1: shell_exec requires approval
    with pytest.raises(PermissionError, match="metachar"):
        asyncio.run(
            reg.call("shell_exec", {"command": "ls && rm -rf /", "timeout": 5})
        )
