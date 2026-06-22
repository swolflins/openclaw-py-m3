"""Phase 16 测试:re-review 第二轮修复的回归覆盖。

覆盖:
- MEM-4  LongTermStore.add() 写入 _ts 时间戳 → LRU 真正生效
- TOOL-1 docker 沙箱注册参数包含加固项
- NEW-1  生产模式无 token 启动被拒绝
- SEC-12 限流中间件、metrics 路径正则降基数
- RT-1   memory scoped 走 asyncio.to_thread
- RT-7+8 CircuitBreaker + tenacity 重试
"""
from __future__ import annotations

import asyncio
import inspect
import re
import time

import pytest


# ─────────────── MEM-4 ───────────────
def _fake_embed(texts):
    """避免下载 sentence-transformers;用 16 维 hash embedding。"""
    out = []
    for t in texts:
        v = [0.0] * 16
        for i, ch in enumerate(t):
            v[i % 16] += (ord(ch) % 13) / 13.0
        out.append(v)
    return out


def test_long_term_add_writes_ts_metadata(tmp_path):
    """MEM-4:add() 必须在 metadata 里写入 _ts,LRU 淘汰才能按时间排序。"""
    from openclaw.memory.long_term import LongTermStore

    store = LongTermStore(
        dir_path=tmp_path / "lt",
        collection="c_lru",
        max_items=10,
        embedding_fn=_fake_embed,
    )
    iid = store.add("hello world", scope="default", metadata={"src": "x"})
    # 反查拿 metadata
    data = store._collection.get(ids=[iid])
    meta = (data.get("metadatas") or [{}])[0] or {}
    assert "_ts" in meta, f"add() 未写入 _ts,LRU 淘汰会退化为 FIFO:meta={meta}"
    assert isinstance(meta["_ts"], (int, float))
    assert meta["_ts"] > 0
    # 范围合理(在最近 60 秒内)
    assert abs(meta["_ts"] - time.time()) < 60


def test_long_term_add_preserves_caller_metadata(tmp_path):
    """MEM-4:用户传进来的 metadata 不能被 _ts 覆盖或丢失。"""
    from openclaw.memory.long_term import LongTermStore

    store = LongTermStore(
        dir_path=tmp_path / "lt",
        collection="c_meta",
        max_items=10,
        embedding_fn=_fake_embed,
    )
    iid = store.add("payload", scope="s1", metadata={"src": "agent", "tag": 42})
    data = store._collection.get(ids=[iid])
    meta = (data.get("metadatas") or [{}])[0] or {}
    assert meta.get("src") == "agent"
    assert meta.get("tag") == 42
    assert meta.get("scope") == "s1"
    assert "_ts" in meta


# ─────────────── TOOL-1 ───────────────
def test_docker_register_has_hardening_kwargs():
    """TOOL-1:register_docker_tools 应有 cpu/pids/ro/user/cap_drop 等安全参数。"""
    from openclaw.tools.builtin import docker as docker_mod

    sig = inspect.signature(docker_mod.register_docker_tools)
    params = sig.parameters
    for k in ("cpu_quota", "cpu_period", "pids_limit", "read_only", "run_as_user", "cap_drop", "no_new_privileges"):
        assert k in params, f"TOOL-1:register_docker_tools 缺少参数 {k}"
    # 安全默认值
    assert params["pids_limit"].default > 0
    assert params["read_only"].default is True
    assert params["cap_drop"].default == ("ALL",)
    assert params["no_new_privileges"].default is True


# ─────────────── TOOL 优化 — 镜像白名单 ───────────────
def test_docker_image_allowlist_blocks_disallowed():
    """TOOL 优化:不在白名单的镜像应被拒绝。"""
    from openclaw.core.errors import ToolError
    from openclaw.tools.builtin.docker import _check_image_allowed, _DEFAULT_ALLOWED_IMAGES

    # 白名单内 → 通过
    _check_image_allowed("python:3.11-slim", _DEFAULT_ALLOWED_IMAGES)
    _check_image_allowed("alpine:3.20", _DEFAULT_ALLOWED_IMAGES)
    # 非法格式 → 拒绝
    with pytest.raises(ToolError, match="格式非法"):
        _check_image_allowed("", _DEFAULT_ALLOWED_IMAGES)
    with pytest.raises(ToolError, match="格式非法"):
        _check_image_allowed("evil; rm -rf /", _DEFAULT_ALLOWED_IMAGES)
    # 不在白名单 → 拒绝
    with pytest.raises(ToolError, match="不在白名单"):
        _check_image_allowed("malware/cryptominer:latest", _DEFAULT_ALLOWED_IMAGES)
    with pytest.raises(ToolError, match="不在白名单"):
        _check_image_allowed("python:2.7", _DEFAULT_ALLOWED_IMAGES)


def test_docker_register_has_allowlist_param():
    """TOOL 优化:register_docker_tools 应有 allowed_images / enforce_allowlist 参数。"""
    from openclaw.tools.builtin import docker as docker_mod

    sig = inspect.signature(docker_mod.register_docker_tools)
    params = sig.parameters
    assert "allowed_images" in params
    assert "enforce_allowlist" in params
    # 默认应该开启
    assert params["enforce_allowlist"].default is True


# ─────────────── NEW-1 ───────────────
def test_production_mode_requires_token(monkeypatch):
    """NEW-1:OPENCLAW_GATEWAY_ENV=production 且无 token → create_app 抛 RuntimeError。"""
    from openclaw.gateway import auth

    # Phase 27 / M9:prod + dev=1 是矛盾,清掉 conftest autouse 注入
    monkeypatch.delenv("OPENCLAW_GATEWAY_DEV", raising=False)
    # 清掉 token + 设 production
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.setenv("OPENCLAW_GATEWAY_ENV", "production")
    # is_production_mode 必须识别
    assert auth.is_production_mode() is True
    # require_token_in_production 必须抛
    with pytest.raises(RuntimeError, match="OPENCLAW_GATEWAY_TOKEN"):
        auth.require_token_in_production()


def test_production_mode_with_token_ok(monkeypatch):
    """NEW-1:production + 配 token → 不抛错,只是短 token 警告。"""
    from openclaw.gateway import auth

    # Phase 27 / M9:prod + dev=1 是矛盾配置,测试需要先清掉 conftest autouse
    # 设的 dev=1(测试装置的全局 dev 模式)
    monkeypatch.delenv("OPENCLAW_GATEWAY_DEV", raising=False)
    monkeypatch.setenv("OPENCLAW_GATEWAY_ENV", "production")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "x" * 40)
    auth.require_token_in_production()  # 不应抛


def test_dev_mode_no_token_no_raise(monkeypatch):
    """NEW-1:dev 模式无 token → 不抛错(向后兼容)。"""
    from openclaw.gateway import auth

    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_ENV", raising=False)
    assert auth.is_production_mode() is False
    auth.require_token_in_production()  # 不应抛


# ─────────────── SEC-12 metrics 降基数 ───────────────
def test_metrics_normalize_path_high_cardinality():
    """SEC-12:_normalize_path 必须把 UUID/hex/长数字序列替换成 {id}。"""
    from openclaw.gateway.metrics import _normalize_path

    # 单段 hex 8+ 字符 → {id}
    assert _normalize_path("/v1/sessions/abc123def456") == "/v1/sessions/{id}"
    # 单段数字 4+ 字符 → {id}
    assert _normalize_path("/v1/memory/1234567890") == "/v1/memory/{id}"
    # 标准 UUID 模式(8-4-4-4-12)被分成多段匹配,每段都归一化(实际效果一致:{id} 占位)
    out_uuid = _normalize_path("/v1/sessions/12345678-1234-1234-1234-123456789012")
    assert "12345678" not in out_uuid
    assert "{id}" in out_uuid
    # 短字面量不应被替换
    assert _normalize_path("/v1/chat") == "/v1/chat"
    assert _normalize_path("/healthz") == "/healthz"
    # 多个段都被高基数正则匹配
    out = _normalize_path("/v1/chat/abc123def456/memory/1234567890")
    assert "{id}" in out
    assert "abc123def456" not in out
    assert "1234567890" not in out


# ─────────────── RT-1 memory 异步 ───────────────
def test_scoped_memory_recent_is_async():
    """RT-1:scoped.ScopedMemory.recent_messages 必须是 async coroutine function。"""
    from openclaw.memory.scoped import ScopedMemory

    assert asyncio.iscoroutinefunction(ScopedMemory.recent_messages)
    assert asyncio.iscoroutinefunction(ScopedMemory.build_messages)
    assert asyncio.iscoroutinefunction(ScopedMemory.append_turn)


# ─────────────── RT-7+8 Provider 熔断 + 重试 ───────────────
def test_router_has_circuit_breaker():
    """RT-7:providers.router 必须有 CircuitBreaker 类的状态机。"""
    from openclaw.providers import router as r

    src = inspect.getsource(r)
    assert "_BreakerState" in src or "CircuitBreaker" in src, "缺少熔断器"
    assert "tenacity" in src, "未使用 tenacity"


def test_router_uses_tenacity_retry():
    """RT-8:acomplete_with_retry 必须走 tenacity.AsyncRetrying。"""
    from openclaw.providers import router as r

    src = inspect.getsource(r)
    assert "AsyncRetrying" in src
    assert "stop_after_attempt" in src
    assert "wait_exponential" in src


# ─────────────── ENG 优化:pip-audit 硬失败 ───────────────
def test_pip_audit_yml_hard_fails_on_cve():
    """ENG 优化:CI 的 pip-audit 步骤必须 '有 CVE 时 exit 1'(硬失败)。"""
    import re
    from pathlib import Path

    yml = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    # 必须有:发现 CVE 时 exit 1(硬失败)
    assert "exit 1" in yml, "pip-audit 应在发现 CVE 时硬失败(exit 1),不能只 warning"
    # 抓 Run pip-audit 步骤的 run 块(到下一个 step 之前)
    m = re.search(r"Run pip-audit.*?run: \|\n(.*?)(?=\n      - |\Z)", yml, re.DOTALL)
    assert m is not None, "找不到 pip-audit 步骤"
    run_block = m.group(1)
    # 关键:pip-audit 行捕获退出码,不能无脑 '|| true' 短路
    assert "pip-audit --strict" in run_block
    assert "AUDIT_EXIT" in run_block or "exit 1" in run_block
    # '|| true' 这种"忽略错误"的语法不应再出现在 run 块里
    assert "|| true" not in run_block, f"pip-audit run 块里还有 '|| true' 短路:{run_block!r}"


# ─────────────── RT-8 续:executor 重试退避 ───────────────
def test_executor_retry_has_backoff():
    """RT-8 续:PlanExecutor._exec_step 重试分支必须 await asyncio.sleep(退避)。"""
    from openclaw.agent import executor as ex

    src = inspect.getsource(ex.PlanExecutor._exec_step)
    # 必须有 sleep + backoff 字样
    assert "asyncio.sleep" in src, "executor._exec_step 仍无退避 sleep,会被毫秒级连发"
    assert "backoff" in src or "0.1" in src or "min(1.0" in src


def test_executor_imports_random_for_jitter():
    """RT-8 续:executor 引入 random 用于抖动。"""
    import openclaw.agent.executor as ex
    import random as _random
    assert hasattr(ex, "random"), "executor 缺 random 模块(用于 jitter)"
    # 确认是同源
    assert ex.random is _random


# ─────────────── SEC-3 shell 安全 ───────────────
def test_shell_uses_shlex_and_shell_false():
    """SEC-3:shell tool 必须用 shlex.split + shell=False。"""
    from openclaw.tools.builtin import shell as shell_mod

    src = inspect.getsource(shell_mod)
    assert "_split_command" in src, "shell.py 应有 _split_command 跨平台包装"
    assert "shell=False" in src
    assert "shell=True" not in src


# ─────────────── SEC-11 异常脱敏 ───────────────
def _strip_docstrings_and_comments(src: str) -> str:
    """从 inspect.getsource 拿到的源码里去掉 docstring + 注释,只看运行时代码。"""
    # 三引号 docstring
    out = re.sub(r'"""[\s\S]*?"""', "", src)
    out = re.sub(r"'''[\s\S]*?'''", "", out)
    # 整行注释
    out = re.sub(r"#[^\n]*", "", out)
    return out


def test_chat_routes_dont_leak_raw_exception():
    """SEC-11:chat.py 运行时代码中不应出现 str(e)/str(exc) 拼到响应。"""
    from openclaw.gateway.routes import chat

    code = _strip_docstrings_and_comments(inspect.getsource(chat))
    # 允许在注释/docstring 出现;只禁运行时代码把 str(e) 拼到响应
    assert "str(e)" not in code, "chat.py 运行时还在用 str(e) 拼到响应"
    assert "str(exc)" not in code, "chat.py 运行时还在用 str(exc) 拼到响应"


def test_channels_routes_dont_leak_raw_exception():
    """SEC-11:channels.py 运行时代码中不应直接 str(e)。"""
    from openclaw.gateway.routes import channels

    code = _strip_docstrings_and_comments(inspect.getsource(channels))
    assert "str(e)" not in code, "channels.py 运行时还在用 str(e)"
    assert "str(exc)" not in code
