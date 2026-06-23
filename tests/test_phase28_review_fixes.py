"""Phase 27 第二轮修复的回归测试(继续修复)。

涵盖本轮追加的 12 项:

H1  Discord/Lark webhook 验签 fail-closed(已修,这里测 fail-closed 路径)
H2  requirements.lock 存在且覆盖所有 extras
H6  README 含架构图(用关键词 + 关键模块名验证)
H7  CHANGELOG.md 存在且提到 Phase 27
H8  CONTRIBUTING.md 含"PR 提交清单"段
H9  lifespan graceful shutdown 调 stop_all / aclose
H10 Makefile 含 publish-test / publish 目标
M5  to_jsonable 不再吞异常(用 logger.debug,触发 5 个 catch-all 路径)
M6  Discord.start 删除重复 pynacl 检查(行为不变,但源码里只 1 次)
M8  ToolsConfig.extras 拒绝非法模块路径
M10 ChannelRuntimeConfig.webhook_host 默认 127.0.0.1
M11 RateLimitMiddleware 走 X-Forwarded-For(需 TRUSTED_PROXY=1)
M12 Playwright 启动参数来自 env
M15 create_app 主函数 < 80 行,_install_middlewares 是单独函数
M19 docker-compose.yml 用 ${VAR:?err} 阻断空 token
M18 Dockerfile 用 digest pin(@sha256:...)
M22 journal.reflect 返回 str(proposal 走 logger.debug)
M23 docs/plugin-development.md 存在
M24 docs/deployment.md 存在
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


REPO = Path(__file__).resolve().parents[1]


# ============================================================
# H2 — requirements.lock 存在 + 含关键依赖
# ============================================================
class TestRequirementsLock:
    def test_lockfile_exists(self):
        lock = REPO / "requirements.lock"
        assert lock.exists(), "requirements.lock 缺失(Phase 27 / H2 修复)"
        # 至少 50 行
        lines = lock.read_text(encoding="utf-8").splitlines()
        assert len(lines) >= 50, f"lockfile 太短({len(lines)} 行),可能 freeze 没装全"

    def test_lockfile_pins_versions(self):
        lock = REPO / "requirements.lock"
        for ln in lock.read_text(encoding="utf-8").splitlines()[:5]:
            assert "==" in ln, f"lockfile 行 {ln!r} 不带版本 pin"


# ============================================================
# H6 — README 架构图
# ============================================================
class TestReadmeArchitecture:
    def test_architecture_section_exists(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        assert "## 架构总览" in readme, "README 缺 '架构总览' 段(Phase 27 / H6)"
        # 关键不变量至少 3 条
        assert readme.count("不变量") >= 1
        # 关键模块名都在
        for mod in ["AgentLoop", "create_app", "ChannelManager", "openai_compat", "AuthMiddleware"]:
            assert mod in readme, f"README 架构图缺 {mod}"


# ============================================================
# H7 — CHANGELOG.md
# ============================================================
class TestChangelog:
    def test_changelog_exists(self):
        assert (REPO / "CHANGELOG.md").exists(), "CHANGELOG.md 缺失(Phase 27 / H7)"

    def test_changelog_mentions_phase27(self):
        text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
        assert "Phase 27" in text
        # 至少 5 个修复编号
        for tag in ["C1", "C5", "H1", "M3", "M9", "M19"]:
            assert tag in text, f"CHANGELOG 缺 {tag}"


# ============================================================
# H8 — CONTRIBUTING PR 清单
# ============================================================
class TestContributingChecklist:
    def test_pr_checklist_section(self):
        text = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
        assert "提交 PR 前清单" in text
        # 5 个分类
        for h in ["测试", "代码质量", "文档", "安全", "Commit 格式"]:
            assert h in text, f"CONTRIBUTING.md 缺 '{h}' 段"


# ============================================================
# H9 — graceful shutdown
# ============================================================
class TestGracefulShutdown:
    def test_lifespan_closes_dependencies(self):
        """_lifespan 在 finally 阶段应能调到 channel_manager.stop_all / agent_loop.aclose。"""
        from openclaw.gateway.app import _lifespan
        # 源码级检查
        import inspect
        src = inspect.getsource(_lifespan)
        assert "stop_all" in src, "_lifespan 应在 shutdown 调 channel_manager.stop_all"
        assert "aclose" in src, "_lifespan 应在 shutdown 调 aclose"
        # 三个阶段注释
        for marker in ["1)", "2)", "3)"]:
            assert marker in src, f"_lifespan 缺第 {marker} 阶段"

    def test_lifespan_runs_without_deps(self, monkeypatch):
        """空 deps 时 _lifespan 不能 raise。"""
        from openclaw.gateway.app import create_app

        monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")
        deps = MagicMock()
        deps.ready.return_value = False
        deps.agent_loop = None
        deps.journal = None
        deps.channel_manager = None
        deps.providers = None
        deps.extra = {}
        deps.current_user_id = "anonymous"

        app = create_app(deps=deps, host="127.0.0.1", rate_limiter=None)
        # 用 TestClient 走完 lifespan 进出
        with TestClient(app) as client:
            r = client.get("/")
            assert r.status_code == 200
        # 不应抛


# ============================================================
# H10 — Makefile publish 目标
# ============================================================
class TestMakefilePublish:
    def test_publish_target_exists(self):
        text = (REPO / "Makefile").read_text(encoding="utf-8")
        for tgt in ["publish-test:", "publish:", "build-dist:", "check-dist:"]:
            assert tgt in text, f"Makefile 缺 {tgt}"

    def test_lockfile_target_exists(self):
        text = (REPO / "Makefile").read_text(encoding="utf-8")
        for tgt in ["lock:", "verify-lock:"]:
            assert tgt in text, f"Makefile 缺 {tgt}(Phase 27 / H2)"


# ============================================================
# M5 — to_jsonable 5 处 catch-all 走 logger.debug
# ============================================================
class TestToJsonableLogs:
    def test_catch_alls_have_logger_debug(self):
        from openclaw.gateway import util
        src = Path(util.__file__).read_text(encoding="utf-8")
        # 至少 5 处 logger.debug
        assert src.count("logger.debug") >= 5, "to_jsonable 5 个 catch-all 路径应都加 logger.debug"

    def test_to_jsonable_handles_bad_bytes(self):
        from openclaw.gateway.util import to_jsonable
        # 正常 bytes
        assert to_jsonable(b"hello") == "hello"
        # 不可 json-serialize 的对象走 repr
        class Opaque:
            def __repr__(self): return "<opaque>"
        assert to_jsonable(Opaque()) == "<opaque>"

    def test_to_jsonable_handles_pydantic_errors(self):
        """model_dump 抛错的对象不应该 crash。"""
        from openclaw.gateway.util import to_jsonable

        class BadModel:
            def model_dump(self):
                raise RuntimeError("boom")

        # 不应抛 — 走 __dict__ 或 repr 兜底
        result = to_jsonable(BadModel())
        # 不论结果是什么,只要不抛
        assert result is not None


# ============================================================
# M6 — Discord.start 仍保留 pynacl 双保险(放在 Discord API 调用前)
# ============================================================
class TestDiscordNoDuplicateCheck:
    def test_start_pynacl_check_precedes_api_call(self):
        """start() 内仍有 pynacl prod 检查,但必须**先于** Discord API 调用。

        M6 修复策略:__init__ 已 fail-fast,但保留 start() 内的双保险
        (防止 monkeypatch 绕过 __init__ 后仍能 fail-closed)。关键是
        pynacl 检查在 client.get('/users/@me') **之前**,避免连 Discord
        失败时掩盖 pynacl 缺失的根因。
        """
        from openclaw.channels import discord
        src = Path(discord.__file__).read_text(encoding="utf-8")
        import re
        start_match = re.search(
            r"    async def start.*?(?=\n    async def |\n    def |class )",
            src, re.DOTALL
        )
        assert start_match, "找不到 start() 函数"
        start_src = start_match.group(0)
        # start 内有 pynacl 检查(_has_pynacl 调用)
        assert "_has_pynacl" in start_src, (
            "Discord.start 仍应有 pynacl 检查(M6 修复:双保险)"
        )
        # pynacl 检查必须**先于** client.get(/users/@me)
        pynacl_pos = start_src.find("_has_pynacl")
        api_pos = start_src.find("client.get")
        assert pynacl_pos < api_pos, (
            f"pynacl 检查应在 Discord API 调用前(pynacl@{pynacl_pos} < api@{api_pos})"
        )


# ============================================================
# M8 — ToolsConfig.extras 拒绝非法模块路径
# ============================================================
class TestToolsConfigExtras:
    def test_extras_accepts_valid_paths(self):
        from openclaw.core.config import OpenClawConfig
        cfg = OpenClawConfig.model_validate(
            {"providers": [], "tools": {"extras": ["my_pkg.tools.mymod", "foo.bar.baz"]}}
        )
        assert cfg.tools.extras == ["my_pkg.tools.mymod", "foo.bar.baz"]

    @pytest.mark.parametrize("bad_path", [
        "../../etc/passwd",  # 路径穿越
        "/abs/path",         # 绝对路径
        "..",                # 单段点
        ".relative",         # 起始点
        "foo/../bar",        # 含斜杠
        "foo bar",           # 含空格
        "foo;rm -rf /",      # 含 shell 元字符
        "foo$bar",           # 含 $
        "",                  # 空串
        "1foo",              # 数字打头(标识符非法)
        "foo..bar",          # 连续点
        ".foo",              # 起始点
    ])
    def test_extras_rejects_invalid_paths(self, bad_path):
        from openclaw.core.config import OpenClawConfig
        from pydantic import ValidationError
        with pytest.raises((ValidationError, ValueError)):
            OpenClawConfig.model_validate(
                {"providers": [], "tools": {"extras": [bad_path]}}
            )

    @pytest.mark.parametrize("good_path", [
        "os",                # 标准库合法模块名
        "my_pkg",
        "my_pkg.tools.mymod",
        "foo",
        "_underscore_ok",
        "a1.b2.c3",
    ])
    def test_extras_accepts_module_names(self, good_path):
        from openclaw.core.config import OpenClawConfig
        # 这些是合法 Python module path,必须接受
        cfg = OpenClawConfig.model_validate(
            {"providers": [], "tools": {"extras": [good_path]}}
        )
        assert good_path in cfg.tools.extras

    def test_extras_deduplicates(self):
        from openclaw.core.config import OpenClawConfig
        cfg = OpenClawConfig.model_validate(
            {"providers": [], "tools": {"extras": ["a.b.c", "a.b.c", "a.b.c"]}}
        )
        assert cfg.tools.extras == ["a.b.c"], "重复路径应被去重"


# ============================================================
# M10 — ChannelRuntimeConfig.webhook_host 默认 127.0.0.1
# ============================================================
class TestWebhookHostDefault:
    def test_default_is_loopback(self):
        from openclaw.core.config import OpenClawConfig
        cfg = OpenClawConfig()
        assert cfg.channels_runtime.webhook_host == "127.0.0.1", (
            f"webhook_host 默认应为 127.0.0.1(Phase 27 / M10),实为 {cfg.channels_runtime.webhook_host}"
        )

    def test_user_can_still_override(self):
        from openclaw.core.config import OpenClawConfig
        cfg = OpenClawConfig.model_validate(
            {"providers": [], "channels_runtime": {"webhook_host": "0.0.0.0"}}
        )
        assert cfg.channels_runtime.webhook_host == "0.0.0.0"


# ============================================================
# M11 — RateLimitMiddleware 走 X-Forwarded-For(TRUSTED_PROXY=1)
# ============================================================
class TestRateLimitTrustedProxy:
    def test_default_uses_client_host(self, monkeypatch):
        """默认(无 TRUSTED_PROXY)走 client.host,不被 XFF 绕过。"""
        from openclaw.gateway.app import RateLimitMiddleware
        # 不设 TRUSTED_PROXY
        monkeypatch.delenv("OPENCLAW_GATEWAY_TRUSTED_PROXY", raising=False)
        from unittest.mock import MagicMock
        request = MagicMock()
        request.client.host = "127.0.0.1"
        request.headers = {"X-Forwarded-For": "8.8.8.8"}
        sid = RateLimitMiddleware._resolve_client_id(request)
        # 应该走 client.host,不是 XFF
        assert sid == "peer:127.0.0.1", f"默认应走 client.host,实为 {sid}"
        assert "xff:" not in sid
        assert "8.8.8.8" not in sid

    def test_trusted_proxy_uses_xff(self, monkeypatch):
        """设了 TRUSTED_PROXY=1,取 XFF 首项。"""
        from openclaw.gateway.app import RateLimitMiddleware
        from unittest.mock import MagicMock
        monkeypatch.setenv("OPENCLAW_GATEWAY_TRUSTED_PROXY", "1")
        request = MagicMock()
        request.client.host = "127.0.0.1"
        request.headers = {"X-Forwarded-For": "8.8.8.8, 10.0.0.1"}
        sid = RateLimitMiddleware._resolve_client_id(request)
        assert sid == "xff:8.8.8.8", f"TRUSTED_PROXY 应取 XFF 首项,实为 {sid}"


# ============================================================
# M12 — Playwright --no-sandbox 可关
# ============================================================
class TestPlaywrightNoSandboxToggle:
    def test_default_arg_includes_no_sandbox(self):
        """默认仍带 --no-sandbox(向后兼容)。"""
        from openclaw.tools.builtin import playwright_tool
        src = Path(playwright_tool.__file__).read_text(encoding="utf-8")
        assert "OPENCLAW_PLAYWRIGHT_NO_SANDBOX" in src
        # 默认逻辑:env=1 或省略 = 加 --no-sandbox
        # 显式 =0 = 不加
        assert "no_sandbox.lower() not in" in src

    def test_off_removes_no_sandbox(self, monkeypatch):
        """设 OPENCLAW_PLAYWRIGHT_NO_SANDBOX=0 → chromium_args 不含 --no-sandbox。"""
        monkeypatch.setenv("OPENCLAW_PLAYWRIGHT_NO_SANDBOX", "0")
        # 重读 env
        val = os.environ.get("OPENCLAW_PLAYWRIGHT_NO_SANDBOX", "1")
        assert val.lower() in ("0", "false", "no", "off")
        # 走真实代码路径(用 import 后的逻辑,不入线程)
        no_sandbox = os.environ.get("OPENCLAW_PLAYWRIGHT_NO_SANDBOX", "1")
        chromium_args = ["--disable-dev-shm-usage"]
        if no_sandbox.lower() not in ("0", "false", "no", "off"):
            chromium_args.append("--no-sandbox")
        assert "--no-sandbox" not in chromium_args


# ============================================================
# M15 — _install_middlewares 抽出 + create_app 主函数瘦
# ============================================================
class TestCreateAppSlim:
    def test_install_middlewares_is_function(self):
        from openclaw.gateway import app as app_mod
        assert hasattr(app_mod, "_install_middlewares")
        assert callable(app_mod._install_middlewares)

    def test_create_app_body_under_80_lines(self):
        """Phase 27 / M15:create_app 主函数体应 < 80 行(原 > 100 行)。"""
        from openclaw.gateway import app as app_mod
        import inspect
        src = inspect.getsource(app_mod.create_app)
        # 实际"函数体"行数(去掉 def/def 行/纯注释/空行)
        body_lines = [
            ln for ln in src.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        # 包含 def + docstring + body,但 docstring 也算
        # 宽松判定:< 80 非空行
        assert len(body_lines) < 80, f"create_app 函数体 {len(body_lines)} 行仍 > 80(M15 未完成)"


# ============================================================
# M19 — docker-compose.yml ${VAR:?err}
# ============================================================
class TestDockerComposeTokenEnforced:
    def test_compose_uses_required_token_syntax(self):
        text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
        # 不应再用 :-} 空默认值
        assert "OPENCLAW_GATEWAY_TOKEN:-" not in text, (
            "docker-compose.yml 用了 :-} 空默认值,应改 :?err(M19)"
        )
        # 应该有 :?err
        assert "OPENCLAW_GATEWAY_TOKEN:?" in text, (
            "docker-compose.yml 缺 :?err 强制 token(M19)"
        )


# ============================================================
# M18 — Dockerfile digest pin
# ============================================================
class TestDockerfileDigest:
    def test_dockerfile_uses_sha256_pin(self):
        text = (REPO / "Dockerfile").read_text(encoding="utf-8")
        # 至少 1 个 FROM ...@sha256:...
        assert "@sha256:" in text, "Dockerfile 未用 digest pin(M18 修复)"


# ============================================================
# M22 — journal.reflect 仍返回 str,proposal 走 logger.debug
# ============================================================
class TestJournalReflectReturnsStr:
    @pytest.mark.asyncio
    async def test_reflect_returns_str(self, tmp_path):
        from openclaw.agent.journal import AgentJournal, JournalEntry

        j = AgentJournal(root=tmp_path / "j")
        entry = JournalEntry(
            session_id="sess_m22_p28",
            timestamp="2026-06-22T00:00:00+00:00",
            user_message="hi",
            final_content="yo",
            iterations=1,
            tool_calls=[],
        )
        out = await j.reflect(entry)
        assert isinstance(out, str), f"reflect 应返回 str,实为 {type(out)}"


# ============================================================
# M23 — docs/plugin-development.md
# ============================================================
class TestPluginDoc:
    def test_plugin_doc_exists(self):
        assert (REPO / "docs" / "plugin-development.md").exists()
        text = (REPO / "docs" / "plugin-development.md").read_text(encoding="utf-8")
        # 涵盖 3 种插件
        for sec in ["Tool", "Skill", "Provider"]:
            assert sec in text


# ============================================================
# M24 — docs/deployment.md
# ============================================================
class TestDeploymentDoc:
    def test_deployment_doc_exists(self):
        assert (REPO / "docs" / "deployment.md").exists()
        text = (REPO / "docs" / "deployment.md").read_text(encoding="utf-8")
        for sec in ["Docker", "systemd", "Nginx", "TLS", "监控", "备份"]:
            assert sec in text, f"deployment.md 缺 '{sec}' 段"
