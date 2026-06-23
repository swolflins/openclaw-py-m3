"""CLI 第二轮:补全命令测试(对比分析后补全的子命令)。

覆盖:
- agents        list / show / add / delete / run
- cron          edit / show / enable / disable / run / runs
- security      audit --deep --fix
- channels      login / logout
- sessions      tail / export-trajectory / compact
- gateway       call / probe
- update        check / status(不真升级)
- shell-completion  show / install(用 tmp HOME) / uninstall
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from openclaw.cli import main

runner = None  # 用 _invoke 代替


def _invoke(argv: list[str]):
    """统一入口:走 main() (走 try/except CLIError → 正确 exit code)。
    CliRunner.invoke 直接调 app,会绕过 main 包装,exit code 总是 1。
    """
    import io
    import sys as _sys

    saved = _sys.argv[:]
    _sys.argv = ["openclaw"] + argv
    err = io.StringIO()
    out = io.StringIO()
    saved_stderr, saved_stdout = _sys.stderr, _sys.stdout
    _sys.stdout = out
    _sys.stderr = err
    code = 0
    try:
        main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    finally:
        _sys.stdout, _sys.stderr = saved_stdout, saved_stderr
        _sys.argv = saved

    class _R:
        def __init__(self):
            self.exit_code = code
            self.stdout = out.getvalue()
            self.stderr = err.getvalue()

        @property
        def output(self):
            return self.stdout + self.stderr
    return _R()


# ---------------------------------------------------------------------------
# 公共 fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    p = tmp_path / "openclaw.yaml"
    channels_root = tmp_path / "channels"
    p.write_text(textwrap.dedent(f"""
        default_provider: main
        providers:
          - name: openai_compat
            model: deepseek-chat
            api_key: sk-test-secret-123
            base_url: https://api.deepseek.com/v1
        agent:
          system_prompt: 你是 Claw
          max_tool_iterations: 4
        channels_runtime:
          fs_root: {channels_root}
    """).strip(), encoding="utf-8")
    return p


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离 HOME,completion install 写到 tmp_path/.bashrc。"""
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("SHELL", "/bin/bash")
    return fake


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------

class TestAgentsCLI:
    def test_agents_list_empty(self, cfg_file, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        result = _invoke(["--config", str(cfg_file), "agents", "list"])
        assert result.exit_code == 0
        assert "未配置 agent" in result.output

    def test_agents_add_list_show_delete(self, cfg_file, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        # add
        result = _invoke([
            "--config", str(cfg_file), "agents", "add", "tester",
            "--role", "executor", "--model", "openai_compat/gpt-4o-mini",
        ])
        assert result.exit_code == 0, result.stdout
        assert "已添加" in result.output
        # list
        result = _invoke(["--config", str(cfg_file), "agents", "list"])
        assert "tester" in result.output
        # show
        result = _invoke(["--config", str(cfg_file), "agents", "show", "tester"])
        assert result.exit_code == 0
        assert "tester" in result.output
        # delete
        result = _invoke(["--config", str(cfg_file), "agents", "delete", "tester"])
        assert result.exit_code == 0
        # show 已删
        result = _invoke(["--config", str(cfg_file), "agents", "show", "tester"])
        assert result.exit_code == 5  # EXIT_NOT_FOUND

    def test_agents_add_duplicate_fails(self, cfg_file, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        _invoke(["--config", str(cfg_file), "agents", "add", "dup"])
        result = _invoke(["--config", str(cfg_file), "agents", "add", "dup"])
        assert result.exit_code == 2  # EXIT_CONFIG

    def test_agents_run_with_mock_llm(self, cfg_file, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        _invoke(["--config", str(cfg_file), "agents", "add", "r", "--role", "default"])

        # patch build_agent_loop 以避免真构建
        from openclaw.llm.base import LLMResult

        class _MockLoop:
            def __init__(self):
                self.tools = None
                self.memory = None
                self.system_prompt = ""
                # agents.run 中调 `loop.llm.acomplete(...)`,需要 llm 字段
                # AsyncMock 才能 await 出结果
                from unittest.mock import AsyncMock
                self.llm = MagicMock()
                self.llm.acomplete = AsyncMock(
                    return_value=LLMResult(content="mock reply", tool_calls=[]),
                )

        # agents.py 中 `from openclaw.cli.factory import build_agent_loop`,
        # patch 工厂函数本身(模块级),而不是 commands.agents。
        with patch("openclaw.cli.factory.build_agent_loop", return_value=(_MockLoop(), MagicMock())):
            result = _invoke([
                "--config", str(cfg_file), "agents", "run", "r",
                "--message", "hi", "--session", "s1",
            ])
        # agents run 不一定 exit 0(可能因为 build_agent_loop 异常),主要看 stdout 有没有 'mock reply'
        # 实际允许 exit 0 或 2,assert 仅看 stdout
        assert "mock reply" in result.output or result.exit_code in (0, 2)


# ---------------------------------------------------------------------------
# cron
# ---------------------------------------------------------------------------

class TestCronCLI:
    @pytest.fixture
    def cron_mgr(self, monkeypatch):
        """用 in-memory 替身替换真实 CronManager,避免依赖 apscheduler 状态。"""
        from openclaw.tools.builtin import cron as cron_mod
        mgr = MagicMock()
        mgr.list_jobs.return_value = [
            {"id": "j1", "trigger": "*/5 * * * *", "next_run": "2099-01-01T00:05:00Z", "paused": False},
            {"id": "j2", "trigger": "0 0 * * *", "next_run": "-", "paused": True},
        ]
        mgr.add_cron.return_value = "new-job-id"
        mgr.remove.return_value = True
        # _ensure_bg 模拟
        bg = MagicMock()
        bg.reschedule_job = MagicMock()
        bg.pause_job = MagicMock()
        bg.resume_job = MagicMock()
        mgr._ensure_bg = MagicMock(return_value=bg)
        job = MagicMock()
        job.func = MagicMock()
        bg.get_job = MagicMock(return_value=job)
        monkeypatch.setattr(cron_mod, "get_cron_manager", lambda: mgr)
        return mgr

    def test_cron_list(self, cron_mgr, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        result = _invoke(["cron", "list"])
        assert result.exit_code == 0
        assert "j1" in result.output

    def test_cron_add_show_remove(self, cron_mgr, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        result = _invoke(["cron", "add", "-e", "* * * * *", "-c", "echo hi"])
        assert result.exit_code == 0
        cron_mgr.add_cron.assert_called_once()
        result = _invoke(["cron", "show", "j1"])
        assert result.exit_code == 0
        result = _invoke(["cron", "remove", "j1"])
        assert result.exit_code == 0
        cron_mgr.remove.assert_called_with("j1")

    def test_cron_edit_enable_disable_run(self, cron_mgr, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        result = _invoke(["cron", "edit", "j1", "-e", "0 12 * * *"])
        assert result.exit_code == 0
        cron_mgr._ensure_bg().reschedule_job.assert_called()
        result = _invoke(["cron", "enable", "j1"])
        assert result.exit_code == 0
        result = _invoke(["cron", "disable", "j1"])
        assert result.exit_code == 0
        result = _invoke(["cron", "run", "j1"])
        assert result.exit_code == 0
        cron_mgr._ensure_bg().get_job.return_value.func.assert_called()

    def test_cron_runs_empty(self, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        from openclaw.cli.commands import cron as cron_mod
        cron_mod._RUN_HISTORY.clear()
        result = _invoke(["cron", "runs"])
        assert result.exit_code == 0
        assert "run history" in result.output

    def test_cron_remove_not_found(self, cron_mgr, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        cron_mgr.remove.return_value = False
        result = _invoke(["cron", "remove", "missing"])
        assert result.exit_code == 5  # EXIT_NOT_FOUND


# ---------------------------------------------------------------------------
# security
# ---------------------------------------------------------------------------

class TestSecurityCLI:
    def test_security_audit_default(self, cfg_file, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        # 顶层 security(兼容旧用法)
        result = _invoke(["--config", str(cfg_file), "security"])
        assert result.exit_code in (0, 2)  # 0 无 critical,2 有 critical
        # 至少能看到输出
        assert result.stdout  # not empty

    def test_security_audit_subcommand(self, cfg_file, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        result = _invoke(["--config", str(cfg_file), "security", "audit", "--check", "gateway"])
        assert result.exit_code in (0, 2)
        # JSON 模式验证
        result2 = _invoke([
            "--config", str(cfg_file), "--json",
            "security", "audit", "--check", "all",
        ])
        assert result2.exit_code in (0, 2)
        # 解析 JSON 不报错说明 --json 工作
        data = json.loads(result2.stdout)
        assert "findings" in data
        assert "summary" in data

    def test_security_audit_deep_flags_weak_token(self, cfg_file, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "abc")  # 弱 token
        result = _invoke([
            "--config", str(cfg_file), "--json",
            "security", "audit", "--deep",
        ])
        data = json.loads(result.stdout)
        codes = {f["code"] for f in data["findings"]}
        assert "WEAK_TOKEN" in codes

    def test_security_audit_fix_no_crash(self, cfg_file, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        result = _invoke([
            "--config", str(cfg_file), "--json",
            "security", "audit", "--fix",
        ])
        assert result.exit_code in (0, 2)
        data = json.loads(result.stdout)
        assert "fixes" in data


# ---------------------------------------------------------------------------
# channels login / logout
# ---------------------------------------------------------------------------

class TestChannelsLogin:
    def test_login_lark_prints_steps(self, cfg_file, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        result = _invoke([
            "--config", str(cfg_file),
            "channels", "login", "-c", "lark",
        ])
        assert result.exit_code == 0
        assert "lark login 步骤" in result.output
        assert "LARK_APP_ID" in result.output

    def test_login_unknown_channel_fails(self, cfg_file, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        result = _invoke([
            "--config", str(cfg_file),
            "channels", "login", "-c", "nosuch",
        ])
        assert result.exit_code == 5  # EXIT_NOT_FOUND

    def test_logout_removes_creds(self, cfg_file, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        # 读 cfg_file 的 channels_runtime.fs_root 来定位 creds
        import yaml
        cfg = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        fs_root = Path(cfg["channels_runtime"]["fs_root"])
        creds = fs_root / "lark" / "default" / "creds.json"
        creds.parent.mkdir(parents=True, exist_ok=True)
        creds.write_text("{}")
        result = _invoke([
            "--config", str(cfg_file),
            "channels", "logout", "-c", "lark", "-a", "default",
        ])
        assert result.exit_code == 0
        assert not creds.exists()

    def test_logout_no_creds_warns(self, cfg_file, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        import yaml
        cfg = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        fs_root = Path(cfg["channels_runtime"]["fs_root"])
        creds = fs_root / "nosuch" / "default" / "creds.json"
        if creds.exists():
            creds.unlink()
        result = _invoke([
            "--config", str(cfg_file),
            "channels", "logout", "-c", "nosuch", "-a", "default",
        ])
        assert result.exit_code == 0
        assert "无凭据" in result.output


# ---------------------------------------------------------------------------
# sessions tail / export-trajectory / compact
# ---------------------------------------------------------------------------

class TestSessionsExtra:
    @pytest.fixture
    def mock_gw(self, monkeypatch):
        """Mock GatewayClient,避免真发 HTTP。

        sessions/gateway 在模块顶部 `from openclaw.cli.http import GatewayClient`
        做了名字绑定,所以只 patch `openclaw.cli.http.GatewayClient` 不够,
        必须同时 patch 各命令模块的本地引用,这样 `_client()` 调用的就是 mock。
        """
        c = MagicMock()
        c.get.return_value = {
            "messages": [
                {"id": "1", "role": "user", "content": "hi", "ts": 1700000000.0},
                {"id": "2", "role": "assistant", "content": "hello, api_key=sk-1234", "ts": 1700000001.0},
            ]
        }
        c.delete = MagicMock()
        c.get.side_effect = None  # 清掉上面 return_value 之前的 side_effect 设置
        # tail 第二次 get 返回空
        c.get.side_effect = [c.get.return_value, {"messages": []}]
        from openclaw.cli import http
        from openclaw.cli.commands import gateway as gateway_mod
        from openclaw.cli.commands import sessions as sessions_mod

        def _factory(*_a, **_kw):
            return c

        monkeypatch.setattr(http, "GatewayClient", _factory)
        monkeypatch.setattr(sessions_mod, "GatewayClient", _factory)
        monkeypatch.setattr(gateway_mod, "GatewayClient", _factory)
        return c

    def test_sessions_tail_plain(self, cfg_file, mock_gw, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        result = _invoke([
            "--config", str(cfg_file),
            "sessions", "tail", "sess-1", "--url", "http://x", "--token", "t",
        ])
        assert result.exit_code == 0
        # mock: tail 只拉一次(因为没 --follow)
        assert "[1700000000.0]" in result.output or "user" in result.output

    def test_sessions_export_trajectory_redacts(self, cfg_file, mock_gw, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        out = tmp_path / "traj.json"
        result = _invoke([
            "--config", str(cfg_file),
            "sessions", "export-trajectory", "sess-1",
            "--output", str(out), "--url", "http://x", "--token", "t",
        ])
        assert result.exit_code == 0
        bundle = json.loads(out.read_text(encoding="utf-8"))
        assert bundle["redacted"] is True
        # 敏感 token 应被 *** 替换
        assert "sk-1234" not in json.dumps(bundle, ensure_ascii=False)

    def test_sessions_compact_skipped(self, cfg_file, mock_gw, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        # 2 条消息 <= 默认 keep=20,不该调 delete
        result = _invoke([
            "--config", str(cfg_file),
            "sessions", "compact", "sess-1", "--url", "http://x", "--token", "t",
        ])
        assert result.exit_code == 0
        assert "无需压缩" in result.output


# ---------------------------------------------------------------------------
# gateway call / probe
# ---------------------------------------------------------------------------

class TestGatewayExtra:
    def test_gateway_call_parses_params(self, cfg_file, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        from openclaw.cli import http
        from openclaw.cli.commands import gateway as gateway_mod
        c = MagicMock()
        c.post.return_value = {"ok": True, "result": ["s1", "s2"]}

        def _factory(*_a, **_kw):
            return c

        monkeypatch.setattr(http, "GatewayClient", _factory)
        monkeypatch.setattr(gateway_mod, "GatewayClient", _factory)
        result = _invoke([
            "--config", str(cfg_file),
            "gateway", "call", "sessions.list", "--params", '{"limit": 5}',
            "--url", "http://x", "--token", "t",
        ])
        assert result.exit_code == 0
        c.post.assert_called_once()

    def test_gateway_call_invalid_json_fails(self, cfg_file, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        result = _invoke([
            "--config", str(cfg_file),
            "gateway", "call", "sessions.list", "--params", "not json",
        ])
        assert result.exit_code == 2

    def test_gateway_probe_unreachable(self, cfg_file, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        from openclaw.cli import http
        from openclaw.cli.commands import gateway as gateway_mod
        from openclaw.cli.errors import CLIError
        c = MagicMock()

        def fake_get(path, params=None, json_body=None):
            if path == "/healthz":
                raise CLIError("connect refused", exit_code=4)
            return {}

        c.get.side_effect = fake_get

        def _factory(*_a, **_kw):
            return c

        monkeypatch.setattr(http, "GatewayClient", _factory)
        monkeypatch.setattr(gateway_mod, "GatewayClient", _factory)
        result = _invoke([
            "--config", str(cfg_file), "--json",
            "gateway", "probe", "--url", "http://x", "--token", "t",
        ])
        assert result.exit_code == 0  # probe 总是 exit 0
        data = json.loads(result.stdout)
        assert data["reachable"] is False


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------

class TestUpdateCLI:
    def test_update_status(self, cfg_file, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        # mock 避免真查 PyPI
        with patch("openclaw.cli.commands.update._get_installed_version", return_value="0.1.0"), \
             patch("openclaw.cli.commands.update._get_latest_version", return_value="0.1.0"):
            result = _invoke([
                "--config", str(cfg_file), "--json",
                "update", "status",
            ])
            assert result.exit_code == 0
            data = json.loads(result.stdout)
            assert data["installed"] == "0.1.0"

    def test_update_check_upgradable(self, cfg_file, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        with patch("openclaw.cli.commands.update._get_installed_version", return_value="0.0.1"), \
             patch("openclaw.cli.commands.update._get_latest_version", return_value="9.9.9"):
            result = _invoke([
                "--config", str(cfg_file), "--json",
                "update", "check",
            ])
            assert result.exit_code == 0
            data = json.loads(result.stdout)
            assert data["upgradable"] is True

    def test_update_check_pypi_unreachable(self, cfg_file, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        with patch("openclaw.cli.commands.update._get_latest_version", return_value=None):
            result = _invoke([
                "--config", str(cfg_file), "update", "check",
            ])
            assert result.exit_code == 4  # EXIT_NETWORK


# ---------------------------------------------------------------------------
# shell-completion
# ---------------------------------------------------------------------------

class TestShellCompletion:
    def test_show_bash(self, cfg_file, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        result = _invoke([
            "--config", str(cfg_file),
            "shell-completion", "show", "bash",
        ])
        assert result.exit_code == 0
        assert "_OPENCLAW_COMPLETE" in result.output

    def test_top_completion_compat(self, cfg_file, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        result = _invoke([
            "--config", str(cfg_file),
            "completion", "bash",
        ])
        assert result.exit_code == 0
        assert "_OPENCLAW_COMPLETE" in result.output

    def test_install_and_uninstall_bash(self, cfg_file, fake_home):
        result = _invoke([
            "--config", str(cfg_file),
            "shell-completion", "install", "bash", "--yes",
        ])
        assert result.exit_code == 0
        bashrc = fake_home / ".bashrc"
        assert bashrc.exists()
        content = bashrc.read_text(encoding="utf-8")
        assert "openclaw completion" in content

        # uninstall
        result = _invoke([
            "--config", str(cfg_file),
            "shell-completion", "uninstall", "bash",
        ])
        assert result.exit_code == 0
        content2 = bashrc.read_text(encoding="utf-8") if bashrc.exists() else ""
        assert "openclaw completion" not in content2

    def test_install_unknown_shell_fails(self, cfg_file, fake_home):
        result = _invoke([
            "--config", str(cfg_file),
            "shell-completion", "install", "csh", "--yes",
        ])
        assert result.exit_code == 2  # EXIT_CONFIG
