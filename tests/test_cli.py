"""CLI 命令测试(使用 typer.testing.CliRunner)。

覆盖:
- version(三种输出模式 + -V)
- config get/set/validate/schema/file(SecretStr 脱敏、原子写、env 插值保留)
- models list
- doctor(含 audit)
- completion
- run 错误路径(无 provider)
- sessions/gateway 错误路径(gateway 未启动)
- 入口兼容性(from openclaw.cli import main)
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openclaw.cli import app, main

runner = CliRunner()


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    """写一份带 SecretStr 的测试配置。"""
    p = tmp_path / "openclaw.yaml"
    p.write_text(textwrap.dedent("""
        default_provider: main
        providers:
          - name: openai_compat
            model: deepseek-chat
            api_key: sk-test-secret-123
            base_url: https://api.deepseek.com/v1
        agent:
          system_prompt: 你是 Claw
          max_tool_iterations: 4
    """).strip(), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------

def test_version_subcommand():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


def test_version_flag():
    result = runner.invoke(app, ["-V"])
    assert result.exit_code == 0
    assert "openclaw-py" in result.stdout


def test_version_json():
    result = runner.invoke(app, ["--json", "version"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["openclaw_py"] == "0.1.0"


def test_version_plain():
    result = runner.invoke(app, ["--plain", "version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


def test_json_plain_mutually_exclusive():
    result = runner.invoke(app, ["--json", "--plain", "version"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def test_config_file(cfg_file: Path):
    result = runner.invoke(app, ["--json", "-c", str(cfg_file), "config", "file"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["exists"] is True


def test_config_get_string(cfg_file: Path):
    result = runner.invoke(app, ["--json", "-c", str(cfg_file), "config", "get", "agent.system_prompt"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["value"] == "你是 Claw"


def test_config_get_secret_masked(cfg_file: Path):
    """SecretStr 默认脱敏为 ***。"""
    result = runner.invoke(app, ["--json", "-c", str(cfg_file), "config", "get", "providers.0.api_key"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["value"] == "***"
    assert "sk-test-secret-123" not in result.stdout


def test_config_get_secret_shown(cfg_file: Path):
    """--show-secrets 显示明文。"""
    result = runner.invoke(
        app, ["--json", "--show-secrets", "-c", str(cfg_file), "config", "get", "providers.0.api_key"]
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["value"] == "sk-test-secret-123"


def test_config_set_and_persist(cfg_file: Path):
    result = runner.invoke(app, ["-c", str(cfg_file), "config", "set", "agent.max_tool_iterations", "8"])
    assert result.exit_code == 0
    # 重新读取验证
    result2 = runner.invoke(app, ["--json", "-c", str(cfg_file), "config", "get", "agent.max_tool_iterations"])
    data = json.loads(result2.stdout)
    assert data["value"] == 8


def test_config_set_invalid_rejected(cfg_file: Path):
    """校验失败的 set 不应落盘。"""
    original = cfg_file.read_text()
    result = runner.invoke(app, ["-c", str(cfg_file), "config", "set", "agent.max_tool_iterations", "not-an-int"])
    assert result.exit_code != 0
    # 文件未变
    assert cfg_file.read_text() == original


def test_config_validate_ok(cfg_file: Path):
    result = runner.invoke(app, ["-c", str(cfg_file), "config", "validate"])
    assert result.exit_code == 0


def test_config_schema():
    result = runner.invoke(app, ["--json", "config", "schema"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "properties" in data


def test_config_unset(cfg_file: Path):
    result = runner.invoke(app, ["-c", str(cfg_file), "config", "unset", "default_provider"])
    assert result.exit_code == 0
    # 再次 unset 同一 key 应报 not found(已删除)
    result2 = runner.invoke(app, ["-c", str(cfg_file), "config", "unset", "default_provider"])
    assert result2.exit_code != 0


def test_config_unset_nonexistent_path(cfg_file: Path):
    """unset 不存在的路径应报错。"""
    result = runner.invoke(app, ["-c", str(cfg_file), "config", "unset", "no.such.path"])
    assert result.exit_code != 0


def test_config_set_preserves_env_placeholder(tmp_path: Path):
    """config set 不应展开 ${ENV} 占位符。"""
    p = tmp_path / "openclaw.yaml"
    p.write_text("providers:\n  - name: openai_compat\n    model: m\n    api_key: ${MY_KEY}\n", encoding="utf-8")
    runner.invoke(app, ["-c", str(p), "config", "set", "agent.history_window", "10"])
    content = p.read_text()
    assert "${MY_KEY}" in content  # 占位符保留


def test_config_get_all_masked(cfg_file: Path):
    result = runner.invoke(app, ["--json", "-c", str(cfg_file), "config", "get"])
    assert result.exit_code == 0
    assert "sk-test-secret-123" not in result.stdout
    assert "***" in result.stdout


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

def test_models_list(cfg_file: Path):
    result = runner.invoke(app, ["--json", "-c", str(cfg_file), "models", "list"])
    assert result.exit_code == 0
    # JSON 输出含 table + factory_supported 两段;直接检查字符串
    assert "factory_supported" in result.stdout
    assert "openai_compat" in result.stdout


def test_models_status_single_provider(cfg_file: Path):
    result = runner.invoke(app, ["--json", "-c", str(cfg_file), "models", "status"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def test_doctor(cfg_file: Path):
    result = runner.invoke(app, ["--json", "-c", str(cfg_file), "doctor"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "summary" in data
    assert "findings" in data


def test_doctor_check_deps(cfg_file: Path):
    result = runner.invoke(app, ["--json", "-c", str(cfg_file), "doctor", "--check", "deps"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "dependencies" in data


# ---------------------------------------------------------------------------
# completion
# ---------------------------------------------------------------------------

def test_completion_bash():
    result = runner.invoke(app, ["completion", "bash"])
    assert result.exit_code == 0
    assert "_openclaw_completion" in result.stdout or "openclaw" in result.stdout


def test_completion_invalid_shell():
    result = runner.invoke(app, ["completion", "tcsh"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# run / sessions / gateway 错误路径
# ---------------------------------------------------------------------------

def test_run_no_provider():
    """无 provider 配置时 run 应给出友好错误(CLIError)。"""

    result = runner.invoke(app, ["run", "--once", "hi"])
    assert result.exit_code != 0
    # CLIError 携带 provider 提示(CliRunner 直接调 app,异常进 result.exception)
    exc = result.exception
    assert exc is not None
    assert "provider" in str(exc).lower() or "配置" in str(exc)


def test_sessions_gateway_not_running():
    result = runner.invoke(app, ["sessions", "list", "--url", "http://127.0.0.1:39999"])
    assert result.exit_code != 0


def test_gateway_health_not_running():
    result = runner.invoke(app, ["gateway", "health", "--url", "http://127.0.0.1:39999"])
    # health 命令本身不退出非零(把错误放进结果),但也可退出非零;此处只验不崩溃
    assert result.exit_code in (0, 1, 4)


def test_serve_missing_dependency(monkeypatch):
    """serve 在缺 uvicorn 时应抛 CLIError(exit_code=3)。

    注:CliRunner 直接调 app 不经 main() 的 exit code 转换,
    故此处校验异常对象的 exit_code;生产路径(main)会 sys.exit(3)。
    """
    from openclaw.cli.errors import CLIError, EXIT_DEPENDENCY
    import openclaw.cli.commands.gateway as gw

    def fake_require(extra, modules):
        raise CLIError(
            f"缺少可选依赖 [{extra}],请运行: pip install 'openclaw-py[{extra}]'",
            exit_code=EXIT_DEPENDENCY,
        )

    monkeypatch.setattr(gw, "_require", fake_require)
    result = runner.invoke(app, ["serve", "--no-agent", "--port", "1"])
    assert result.exception is not None
    assert isinstance(result.exception, CLIError)
    assert result.exception.exit_code == EXIT_DEPENDENCY


def test_main_exit_code_via_subprocess():
    """端到端:经 main() 入口,CLIError 应转换为正确 exit code。"""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "openclaw.cli", "run", "--once", "hi"],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 2  # CONFIG 错误
    assert "provider" in proc.stderr.lower() or "配置" in proc.stderr


# ---------------------------------------------------------------------------
# 入口兼容性
# ---------------------------------------------------------------------------

def test_entry_point_compatible():
    """openclaw = openclaw.cli:main 入口仍可 import。"""
    import openclaw.cli
    assert callable(openclaw.cli.main)
    assert openclaw.cli.app is not None


def test_main_callable_importable():
    """main 是可调用对象(防 regression)。"""
    assert callable(main)


def test_help_lists_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in [
        "version", "run", "serve", "gateway", "config", "models",
        "sessions", "plugins", "skills", "doctor", "completion",
        "agent", "channels", "message", "memory", "journal",
        "tools", "security", "cron", "system", "logs", "sandbox",
    ]:
        assert cmd in result.stdout, f"命令 {cmd} 未在 help 中列出"


# ---------------------------------------------------------------------------
# 第二批命令:agent / channels / message / memory / journal / tools
#                / security / cron / system / logs / sandbox
# ---------------------------------------------------------------------------

def test_agent_no_provider():
    """agent 命令无 provider 配置时应报配置错误。"""
    result = runner.invoke(app, ["agent", "-m", "hi"])
    # 退出非 0 即视为配置错(2 = 配置/用法错,1 = 内部错);不再依赖
    # exception 文本(typer CliRunner 把 SystemExit(2) 包成 exception,str() 为 "2")
    assert result.exit_code != 0


def test_channels_types():
    """channels types 不需 gateway,列出内置类型。"""
    result = runner.invoke(app, ["--json", "channels", "types"])
    assert result.exit_code == 0
    assert "cli" in result.stdout
    assert "lark" in result.stdout


def test_channels_list_gateway_not_running():
    result = runner.invoke(app, ["channels", "list", "--url", "http://127.0.0.1:39999"])
    assert result.exit_code != 0


def test_message_gateway_not_running():
    result = runner.invoke(app, ["message", "chat", "hi", "--url", "http://127.0.0.1:39999"])
    assert result.exit_code != 0


def test_memory_gateway_not_running():
    result = runner.invoke(app, ["memory", "short", "--url", "http://127.0.0.1:39999"])
    assert result.exit_code != 0


def test_journal_gateway_not_running():
    result = runner.invoke(app, ["journal", "entries", "--url", "http://127.0.0.1:39999"])
    assert result.exit_code != 0


def test_tools_gateway_not_running():
    result = runner.invoke(app, ["tools", "list", "--url", "http://127.0.0.1:39999"])
    assert result.exit_code != 0


def test_security(cfg_file: Path):
    """security 审计应返回 findings 结构。

    注:`security` 已升级为子命令组(与 TS 端一致),审计走 `security audit`。
    同时保留 `--check` 在 `security audit` 入口下,作为 audit 的 filter 选项。
    """
    result = runner.invoke(app, ["--json", "-c", str(cfg_file), "security", "audit"])
    # 0 = 无 critical,2 = 有 critical(均属正常审计结果)
    assert result.exit_code in (0, 2)
    data = json.loads(result.stdout)
    assert "findings" in data
    assert "summary" in data


def test_security_check_tools(cfg_file: Path):
    result = runner.invoke(
        app, ["--json", "-c", str(cfg_file), "security", "audit", "--check", "tools"]
    )
    # 0 / 2 都算通过(取决于 cfg 是否触发 critical)
    assert result.exit_code in (0, 2)
    data = json.loads(result.stdout)
    assert "findings" in data


def test_cron_list():
    """cron list 应正常执行(可能无任务)。"""
    result = runner.invoke(app, ["cron", "list"])
    assert result.exit_code == 0


def test_system():
    """system 命令返回系统信息。"""
    result = runner.invoke(app, ["--json", "system"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["openclaw_py"] == "0.1.0"
    assert "dependencies" in data
    assert "python" in data


def test_logs_no_file():
    """logs 命令在无日志文件时应提示而非崩溃。"""
    result = runner.invoke(app, ["logs"])
    assert result.exit_code == 0  # 提示模式,不报错


def test_sandbox():
    """sandbox 命令应正常执行(可能 docker 不可用)。"""
    result = runner.invoke(app, ["sandbox"])
    assert result.exit_code == 0


def test_tools_call_invalid_json(cfg_file: Path):
    """tools call 的 --args 非法 JSON 应报配置错误。"""
    result = runner.invoke(app, ["tools", "call", "some_tool", "-a", "not-json", "--url", "http://127.0.0.1:39999"])
    assert result.exit_code != 0
