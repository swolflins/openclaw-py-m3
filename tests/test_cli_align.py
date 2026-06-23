"""CLI 对齐新增命令测试。

覆盖本次为对齐 openclaw 增加的命令:
- status / health (顶层聚合)
- tasks (本地任务管理)
- secrets (本地 .env 管理)
- mcp tools (MCP 工具列表)
- channels lark status (飞书 WS 状态)
- python -m openclaw 入口
"""
from __future__ import annotations

import json

from typer.testing import CliRunner

from openclaw.cli import app

runner = CliRunner()


def test_python_m_entrypoint():
    """``python -m openclaw --help`` 可用。"""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "openclaw", "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0
    assert "Usage" in proc.stdout


def test_status_top_level(monkeypatch, tmp_path):
    """顶层 status 命令返回聚合结构(即便 gateway 未运行也不崩溃)。"""
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(tmp_path / "nope.yaml"))
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "url" in data
    assert data.get("gateway_error") or data.get("gateway")


def test_health_top_level(monkeypatch, tmp_path):
    """顶层 health 命令检查 /healthz /readyz。"""
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(tmp_path / "nope.yaml"))
    result = runner.invoke(app, ["--json", "health"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data.get("healthy") is False  # gateway 未启动


def test_tasks_lifecycle(tmp_path, monkeypatch):
    """tasks add / list / show / done / delete 完整生命周期。"""
    tasks_file = tmp_path / "tasks.json"
    monkeypatch.setenv("OPENCLAW_TASKS_PATH", str(tasks_file))

    result = runner.invoke(app, ["tasks", "list", "--status", "all"])
    assert result.exit_code == 0
    assert "0" in result.stdout

    result = runner.invoke(app, ["tasks", "add", "对齐 CLI", "-d", "补齐缺失命令"])
    assert result.exit_code == 0
    task_id = result.stdout.strip().split("(")[1].split(")")[0]

    result = runner.invoke(app, ["tasks", "list"])
    assert result.exit_code == 0
    assert "对齐 CLI" in result.stdout

    result = runner.invoke(app, ["tasks", "show", task_id])
    assert result.exit_code == 0
    assert "对齐 CLI" in result.stdout

    result = runner.invoke(app, ["tasks", "done", task_id])
    assert result.exit_code == 0

    result = runner.invoke(app, ["tasks", "list", "--status", "done"])
    assert result.exit_code == 0
    assert "done" in result.stdout

    result = runner.invoke(app, ["tasks", "delete", task_id])
    assert result.exit_code == 0



def test_secrets_lifecycle(tmp_path, monkeypatch):
    """secrets set / list / get / unset 完整生命周期。"""
    env_file = tmp_path / ".env"
    monkeypatch.setenv("OPENCLAW_SECRETS_PATH", str(env_file))

    result = runner.invoke(app, ["secrets", "set", "TEST_KEY", "secret_value"])
    assert result.exit_code == 0

    result = runner.invoke(app, ["secrets", "list"])
    assert result.exit_code == 0
    assert "TEST_KEY" in result.stdout
    assert "***" in result.stdout

    result = runner.invoke(app, ["secrets", "get", "TEST_KEY"])
    assert result.exit_code == 0
    assert "***" in result.stdout

    result = runner.invoke(app, ["secrets", "unset", "TEST_KEY"])
    assert result.exit_code == 0

    result = runner.invoke(app, ["secrets", "get", "TEST_KEY"])
    assert result.exit_code != 0


def test_mcp_tools_list():
    """mcp tools 列出内置工具。"""
    result = runner.invoke(app, ["mcp", "tools"])
    assert result.exit_code == 0
    assert "echo" in result.stdout


def test_channels_lark_status(monkeypatch, tmp_path):
    """channels lark status 检查凭据(不依赖真实 WS)。"""
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(tmp_path / "nope.yaml"))
    monkeypatch.delenv("LARK_APP_ID", raising=False)
    monkeypatch.delenv("LARK_APP_SECRET", raising=False)
    result = runner.invoke(app, ["--json", "channels", "lark", "status"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["creds_ok"] is False


def test_exit_codes_provider_plugin_tool():
    """退出码细化:Provider/Plugin/Tool 错误映射到 79/80/81。"""
    from openclaw.cli.errors import (
        EXIT_CONFIG,
        EXIT_DEPENDENCY,
        EXIT_NETWORK,
        EXIT_NOT_FOUND,
        EXIT_PLUGIN,
        EXIT_PROVIDER,
        EXIT_TOOL_VALIDATION,
        EXIT_UNKNOWN,
        _openclaw_error_exit_code,
    )

    assert EXIT_UNKNOWN == 1
    assert EXIT_CONFIG == 2
    assert EXIT_DEPENDENCY == 3
    assert EXIT_NETWORK == 4
    assert EXIT_NOT_FOUND == 5
    assert EXIT_PROVIDER == 79
    assert EXIT_PLUGIN == 80
    assert EXIT_TOOL_VALIDATION == 81

    class ProviderError(Exception):
        pass

    class PluginError(Exception):
        pass

    class ToolValidationError(Exception):
        pass

    assert _openclaw_error_exit_code(ProviderError()) == EXIT_PROVIDER
    assert _openclaw_error_exit_code(PluginError()) == EXIT_PLUGIN
    assert _openclaw_error_exit_code(ToolValidationError()) == EXIT_TOOL_VALIDATION
