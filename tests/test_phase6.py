"""Phase 6 单测:RateLimiter / AutoReplyManager / Skills。

全部离线,不需要网络。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from openclaw.core.rate_limit import RateLimiter
from openclaw.core.auto_reply import (
    AutoReplyConfig,
    AutoReplyManager,
)
from openclaw.core.skills import (
    Skill,
    SkillAPI,
    SkillRegistry,
    load_skills,
)
from openclaw.tools.registry import ToolCategory, ToolPermission, ToolRegistry


# ---------------- RateLimiter ----------------

def test_rate_limiter_basic():
    rl = RateLimiter(rate=1.0, burst=3)
    # 3 个连续放行
    assert rl.allow("u:alice")
    assert rl.allow("u:alice")
    assert rl.allow("u:alice")
    # 第 4 个拒绝
    assert not rl.allow("u:alice")
    # 等 1s 拿回 1 个
    time.sleep(1.05)
    assert rl.allow("u:alice")


def test_rate_limiter_separate_keys():
    rl = RateLimiter(rate=1.0, burst=2)
    assert rl.allow("u:a")
    assert rl.allow("u:a")
    assert not rl.allow("u:a")
    # b 独立
    assert rl.allow("u:b")
    assert rl.allow("u:b")


def test_rate_limiter_retry_after():
    rl = RateLimiter(rate=1.0, burst=1)
    rl.allow("u:a")
    ra = rl.retry_after("u:a")
    assert 0.5 < ra <= 1.0


def test_rate_limiter_persist(tmp_path: Path):
    p = tmp_path / "rl.db"
    rl = RateLimiter(rate=0.1, burst=2, persist_path=p)
    rl.allow("u:a")
    rl.allow("u:a")
    rl.snapshot()
    rl.close()
    # 重新打开
    rl2 = RateLimiter(rate=0.1, burst=2, persist_path=p)
    snap = rl2.snapshot()
    assert "u:a" in snap
    # burst 只剩 2,消耗后应该是 0
    rl2.allow("u:a")
    assert not rl2.allow("u:a")
    rl2.close()


def test_rate_limiter_reset():
    rl = RateLimiter(rate=1.0, burst=1)
    rl.allow("u:a")
    assert not rl.allow("u:a")
    rl.reset("u:a")
    assert rl.allow("u:a")


def test_rate_limiter_invalid_args():
    with pytest.raises(ValueError):
        RateLimiter(rate=0)
    with pytest.raises(ValueError):
        RateLimiter(rate=1, burst=0)


def test_rate_limiter_async():
    async def main():
        rl = RateLimiter(rate=10, burst=2)
        assert await rl.aallow("u:a")
        assert await rl.aallow("u:a")
        assert not await rl.aallow("u:a")
    asyncio.run(main())


# ---------------- AutoReplyManager ----------------

def _dec(mgr, user, ch, text, **kw):
    return asyncio.run(mgr.decide(user, ch, text, **kw))


def test_auto_reply_blacklist():
    mgr = AutoReplyManager(AutoReplyConfig(
        triggers=["bot"],
        blacklist=[r"rm\s+-rf"],
    ))
    d = _dec(mgr, "u1", "telegram", "rm -rf /")
    assert not d.passthrough
    assert "blacklist" in d.reason


def test_auto_reply_quiet_hours():
    mgr = AutoReplyManager(AutoReplyConfig(
        triggers=["bot"],
        quiet_hours=("00:00", "23:59"),
    ))
    d = _dec(mgr, "u1", "telegram", "bot help")
    assert not d.passthrough
    assert "quiet" in d.reason


def test_auto_reply_template():
    mgr = AutoReplyManager(AutoReplyConfig(
        triggers=["bot"],
        templates={"help": "我在线,需要啥?"},
    ))
    d = _dec(mgr, "u1", "telegram", "help me")
    assert d.reply == "我在线,需要啥?"
    assert not d.passthrough


def test_auto_reply_mention():
    mgr = AutoReplyManager(AutoReplyConfig(
        triggers=["bot"],
    ))
    d = _dec(mgr, "u1", "feishu", "你好", metadata={"mentioned": True})
    assert d.passthrough
    assert d.prompt_prefix and "feishu" in d.prompt_prefix


def test_auto_reply_dm():
    mgr = AutoReplyManager(AutoReplyConfig(
        triggers=["bot"],
        auto_in_dm=True,
    ))
    d = _dec(mgr, "u1", "feishu", "在么", metadata={"is_dm": True})
    assert d.passthrough


def test_auto_reply_not_addressed():
    mgr = AutoReplyManager(AutoReplyConfig(
        triggers=["bot"],
        auto_in_dm=False,
        auto_when_mentioned=False,
    ))
    d = _dec(mgr, "u1", "feishu", "闲聊,不 @ 你")
    assert not d.passthrough
    assert "not addressed" in d.reason


def test_auto_reply_rate_limit_user():
    mgr = AutoReplyManager(AutoReplyConfig(
        triggers=["bot"],
        rate_per_user=RateLimiter(rate=0.1, burst=1),
    ))
    a1 = _dec(mgr, "u1", "telegram", "bot 1")
    a2 = _dec(mgr, "u1", "telegram", "bot 2")
    assert a1.passthrough
    assert not a2.passthrough
    assert "rate" in a2.reason


def test_auto_reply_rate_limit_channel():
    mgr = AutoReplyManager(AutoReplyConfig(
        triggers=["bot"],
        rate_per_channel=RateLimiter(rate=0.1, burst=1),
    ))
    _dec(mgr, "u1", "telegram", "bot 1")
    d = _dec(mgr, "u2", "telegram", "bot 2")
    assert not d.passthrough
    assert "channel" in d.reason


def test_auto_reply_is_addressed_custom():
    def my_check(user, ch, text, meta):
        return "crazy" in text
    mgr = AutoReplyManager(AutoReplyConfig(is_addressed=my_check))
    d = _dec(mgr, "u1", "telegram", "I am crazy")
    assert d.passthrough
    d2 = _dec(mgr, "u1", "telegram", "hello")
    assert not d2.passthrough


def test_auto_reply_stats():
    mgr = AutoReplyManager(AutoReplyConfig(triggers=["bot"], blacklist=[r"rm\s+-rf"]))
    _dec(mgr, "u1", "c1", "bot 1")
    _dec(mgr, "u2", "c1", "random chatter")
    _dec(mgr, "u3", "c1", "rm -rf /")
    s = mgr.stats()
    assert s["allow"] >= 1
    assert s["skipped"] >= 1
    assert s["block_blacklist"] >= 1


# ---------------- Skills ----------------

def test_parse_front_matter():
    from openclaw.core.skills import _parse_front_matter
    md = "---\nname: foo\ntriggers: [a, b]\n---\nbody\n"
    meta, body = _parse_front_matter(md)
    assert meta == {"name": "foo", "triggers": ["a", "b"]}
    assert body == "body\n"


def test_load_skills_from_examples(tmp_path: Path):
    # 用 examples/skills 测试(项目自带)
    repo = Path(__file__).resolve().parent.parent
    examples_dir = repo / "examples" / "skills"
    if not examples_dir.exists():
        pytest.skip("examples/skills 目录不存在")
    sreg = load_skills(examples_dir)
    names = {s.name for s in sreg.skills()}
    assert {"joke", "weather", "system_status"}.issubset(names)


def test_skill_api_tool_and_prompt():
    reg = ToolRegistry()
    sreg = SkillRegistry()
    sk = Skill(name="demo", version="0.1.0", description="demo")
    api = SkillAPI(sk, reg)

    @api.tool(description="demo tool", category=ToolCategory.UTILITY, permission=ToolPermission.SAFE)
    def hello(name: str = "world") -> str:
        return f"hi {name}"

    api.inject_prompt("always say hi")
    sreg.add(sk)  # 不加载 skill.py,直接手动 add(模拟纯 SKILL.md 的场景)

    # 工具注册到共享 reg
    out = asyncio.run(reg.call("hello", {"name": "alice"}))
    assert out == "hi alice"
    # prompt 已 inject
    assert "always say hi" in sk.prompt_injections


def test_skill_load_full():
    """从临时目录构造一个完整 skill,验证 loader + register + prompt。"""
    tmp = Path(tmpfile_mkdtemp_safe())
    skill_dir = tmp / "tmp_skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: tmp_skill\nversion: 0.0.1\ndescription: tmp\ntriggers: [tmp]\n---\n# Tmp\n",
        encoding="utf-8",
    )
    (skill_dir / "skill.py").write_text(
        "from openclaw.core.skills import SkillAPI\n"
        "from openclaw.tools.registry import ToolCategory, ToolPermission\n"
        "def register(api: SkillAPI) -> None:\n"
        "    @api.tool(description='echo', category=ToolCategory.UTILITY, permission=ToolPermission.SAFE)\n"
        "    def echo_t(text: str = 'x') -> str:\n"
        "        return 'echo:' + text\n"
        "    api.inject_prompt('use echo_t')\n",
        encoding="utf-8",
    )
    reg = ToolRegistry()
    sreg = load_skills(tmp, registry=reg)
    assert sreg.get("tmp_skill") is not None
    assert "echo_t" in {t.name for t in reg.list_tools()}
    out = asyncio.run(reg.call("echo_t", {"text": "hi"}))
    assert out == "echo:hi"
    import shutil
    shutil.rmtree(tmp)


def tmpfile_mkdtemp_safe() -> str:
    import tempfile
    return tempfile.mkdtemp(prefix="openclaw_skill_test_")


def test_skill_load_missing_name(tmp_path: Path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\ndescription: no name\n---\n", encoding="utf-8")
    sreg = load_skills(tmp_path)
    assert sreg.get("bad") is None
