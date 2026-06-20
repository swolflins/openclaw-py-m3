"""AutoReplyManager:在 AgentLoop 之前做消息决策(Phase 6)。

能力:
- 黑名单:包含关键词/正则直接拒
- 白名单:仅当触发(关键词命中 / @bot / 私聊 / 自定义 is_addressed)才交给 Agent
- 限流:对 user_id / channel 做 token bucket
- 模板回复:命中关键词时,直接给模板,不再调 LLM
- quiet hours:夜里静默(配置时区)
- 上下文注入:把 user/channel 信息拼到 prompt 前缀

典型用法(在 channel 里):
    arm = AutoReplyManager(rl=RateLimiter(0.2, 3), ...)
    decision = await arm.decide(user_id, channel, text, metadata={...})
    if decision.reply is not None:
        await channel.send(session_id, decision.reply)
        return
    if not decision.passthrough:
        return  # 静默丢弃(被黑名单/quiet hours 等)
    resp = await agent.handle(...)
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from openclaw.core.logging import get_logger
from openclaw.core.rate_limit import RateLimiter

logger = get_logger(__name__)


# 触发回调
IsAddressedFn = Callable[[str, str, str, dict[str, Any]], bool]
"""(user_id, channel, text, metadata) -> 是否认为这条消息是"对机器人说的" """


@dataclass
class AutoReplyConfig:
    # 白名单关键词:命中其一即触发
    triggers: list[str] = field(default_factory=list)
    # 黑名单关键词(正则):命中其一直接丢弃
    blacklist: list[str] = field(default_factory=list)
    # 模板回复: {关键词: 回复文本}   命中后直接给模板,不再调 LLM
    templates: dict[str, str] = field(default_factory=dict)
    # 是否在私聊时自动触发(channel=="dm" 或 metadata.is_dm)
    auto_in_dm: bool = True
    # 是否在群聊中被 @ 时触发(从 metadata 读 "mentioned")
    auto_when_mentioned: bool = True
    # CH-2:user-level allowFrom 白名单 — None/空 = 不限制;非空 = 只允许列表中的 user_id
    allow_from: list[str] = field(default_factory=list)
    # 限流(每个 key 一行)
    rate_per_user: Optional[RateLimiter] = None
    rate_per_channel: Optional[RateLimiter] = None
    # 静默时段(本地时间):"23:00" 到 "07:00"
    quiet_hours: Optional[tuple[str, str]] = None
    # 自定义判定回调(覆盖白名单/私聊/@逻辑)
    is_addressed: Optional[IsAddressedFn] = None
    # prompt 前缀:可在 system_prompt 前面加一段上下文
    prompt_prefix_template: str = "[上下文] channel={channel} user={user_id} ts={ts}\n"


@dataclass
class AutoReplyDecision:
    """decide() 的输出。"""
    # True 表示交给 AgentLoop.handle() 继续处理;False 表示不再处理
    passthrough: bool
    # 非 None 时直接发回用户(模板回复)
    reply: Optional[str] = None
    # 给 LLM 的 prompt 前缀(由 caller 拼到 system_prompt)
    prompt_prefix: Optional[str] = None
    # 决策原因(用于日志/调试)
    reason: str = ""


def _parse_hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _in_quiet(now_minute: int, q: tuple[str, str]) -> bool:
    a, b = _parse_hhmm(q[0]), _parse_hhmm(q[1])
    if a == b:
        return False
    if a < b:
        return a <= now_minute < b
    # 跨天 23:00 -> 07:00
    return now_minute >= a or now_minute < b


class AutoReplyManager:
    """在 LLM 之前的消息路由器。"""

    def __init__(self, cfg: Optional[AutoReplyConfig] = None) -> None:
        self.cfg = cfg or AutoReplyConfig()
        self._trig_re = [
            re.compile(re.escape(t)) for t in self.cfg.triggers
        ]
        self._blk_re = [
            re.compile(p) for p in self.cfg.blacklist
        ]
        # 统计
        self._stats = {
            "allow": 0, "block_blacklist": 0, "block_quiet": 0,
            "block_rate_user": 0, "block_rate_channel": 0,
            "template": 0, "whitelist": 0, "skipped": 0,
        }

    # ----- 决策入口 -----

    async def decide(
        self,
        user_id: str,
        channel: str,
        text: str,
        metadata: Optional[dict[str, Any]] = None,
        now: Optional[_dt.datetime] = None,
    ) -> AutoReplyDecision:
        meta = metadata or {}
        cfg = self.cfg
        now = now or _dt.datetime.now()

        # 0) CH-2:user allowFrom 白名单(空 = 不限制;非空 = 只放行列表中的 user_id)
        if cfg.allow_from and user_id not in cfg.allow_from:
            self._stats["block_allow_from"] = self._stats.get("block_allow_from", 0) + 1
            return AutoReplyDecision(False, reason=f"user {user_id} not in allow_from")

        # 1) 黑名单
        for rx in self._blk_re:
            if rx.search(text):
                self._stats["block_blacklist"] += 1
                return AutoReplyDecision(False, reason=f"blacklist matched {rx.pattern}")

        # 2) 静默时段
        if cfg.quiet_hours is not None and _in_quiet(now.hour * 60 + now.minute, cfg.quiet_hours):
            self._stats["block_quiet"] += 1
            return AutoReplyDecision(False, reason="quiet hours")

        # 3) 模板回复(白名单关键词触发,且配置了模板)
        for kw, tmpl in cfg.templates.items():
            if kw in text:
                self._stats["template"] += 1
                return AutoReplyDecision(
                    passthrough=False,
                    reply=tmpl.format(user=user_id, channel=channel, text=text),
                    reason=f"template matched {kw!r}",
                )

        # 4) 自定义判定
        addressed = False
        if cfg.is_addressed is not None:
            try:
                addressed = bool(cfg.is_addressed(user_id, channel, text, meta))
            except Exception:
                logger.exception("is_addressed callback failed")
                addressed = False
        else:
            # 默认:关键词白名单 / @bot / 私聊
            if any(rx.search(text) for rx in self._trig_re):
                addressed = True
                self._stats["whitelist"] += 1
            elif cfg.auto_in_dm and meta.get("is_dm"):
                addressed = True
            elif cfg.auto_when_mentioned and meta.get("mentioned"):
                addressed = True

        if not addressed:
            self._stats["skipped"] += 1
            return AutoReplyDecision(False, reason="not addressed")

        # 5) 限流
        if cfg.rate_per_channel is not None and not cfg.rate_per_channel.allow(f"ch:{channel}"):
            self._stats["block_rate_channel"] += 1
            return AutoReplyDecision(
                passthrough=False,
                reply="(频道流量过大,稍后再说)",
                reason="channel rate-limited",
            )
        if cfg.rate_per_user is not None and not cfg.rate_per_user.allow(f"u:{user_id}"):
            self._stats["block_rate_user"] += 1
            return AutoReplyDecision(
                passthrough=False,
                reply="(你说话太快啦,稍等一下)",
                reason="user rate-limited",
            )

        # 6) 放行,拼 prompt 前缀
        prefix = cfg.prompt_prefix_template.format(
            user_id=user_id, channel=channel,
            ts=now.isoformat(timespec="seconds"),
        )
        self._stats["allow"] += 1
        return AutoReplyDecision(
            passthrough=True,
            prompt_prefix=prefix,
            reason="addressed + under limit",
        )

    # ----- 辅助 -----

    def stats(self) -> dict[str, int]:
        return dict(self._stats)
