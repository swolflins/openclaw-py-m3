"""飞书 (Lark) 消息渠道。

默认走长连接(WebSocket),无需公网 IP 即可接收消息;
如果 lark-oapi 不可用或没装,该模块退化为「占位实现」,只 import 不报错,
由 CLI 入口在启动前检测并提示。

依赖: pip install lark-oapi

Phase 31 / 参考 Hermes Feishu adapter 的优化项
-----------------------------------------------
1. **Persistent dedup state**: 飞书会重发同一条 message_id(网络抖动/重启),
   把已见 ID 落盘 JSON,重启后跳过,避免重复回复。LRU 上限 ``DEDUP_CACHE_SIZE``。
2. **Per-chat 串行锁**: 同一 chat_id 的消息排队处理,避免并发乱序
   (Hermes 的 ``createChatQueue`` 语义)。锁有 LRU 上限,常驻活跃 chat 不被驱逐。
3. **DM allowlist**: ``LARK_ALLOWED_USERS`` 配置 sender open_id 白名单,
   群消息走 ``LARK_GROUP_POLICY=open|allowlist|disabled`` 策略。
4. **Reaction 事件 → 合成 text 事件**: 把 ``im.message.reaction.created/deleted``
   路由成 ``reaction:added:Typing`` 文本,让 agent 能感知用户对 bot 消息的表态。
5. **Card action → 合成 COMMAND 事件**: 卡片按钮点击转 ``/card <action>`` 命令。
6. **Processing reaction**: 派发前后 add/remove Typing reaction,失败时改 CrossMark。
7. **Webhook 异常追踪 + verification token 校验 API**: 暴露
   ``record_webhook_anomaly`` / ``verify_webhook_token`` 静态/实例方法,
   未来启 Webhook 模式时直接复用。
8. **post 富文本解析**: 支持 at + 文本行,把 ``@_user_xxx`` 替换成 @昵称后再 strip。

Webhook 模式 (LARK_USE_WS=false) 当前仍抛 NotImplementedError,但 verification
token 校验、异常追踪、签名校验等子模块已可在路由层(未来加 aiohttp app)直接复用。
"""
from __future__ import annotations

import asyncio
import collections
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from openclaw.agent.loop import AgentLoop
from openclaw.channels.base import BaseChannel
from openclaw.config.settings import LarkSettings

logger = logging.getLogger(__name__)

# CH-1:用来区分"未拉取"和"已拉取到 None"。None 在业务上是合法值(可能后端返 code!=0),
# 启动期 fail-fast 靠"还是 sentinel"判断,这样能精准区分拉没拉过。
class _UnsetType:
    """单例 sentinel:表示 bot_open_id 还没拉过。"""

    _instance: "_UnsetType | None" = None

    def __new__(cls) -> "_UnsetType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<UNSET>"

    def __bool__(self) -> bool:  # 让 if bot_open_id 永远 False(未拉过时视作空)
        return False


_UNSET = _UnsetType()


# ─────────────────────── Phase 31 常量 ───────────────────────

# 去重缓存容量:超过就 LRU 驱逐最旧 ID(并落盘时截断)。
DEDUP_CACHE_SIZE = 10_000
# 去重 ID 有效期(秒);TTL<=0 表示永不过期。
DEDUP_TTL_SECONDS = 24 * 3600
# 持久化文件路径(可被 OPENCLAW_LARK_DEDUP_PATH 覆盖)。
DEDUP_DEFAULT_PATH = "~/.openclaw/lark_seen_message_ids.json"

# Per-chat 锁最大缓存数(防止长跑 gateway 内存膨胀);活跃锁永不被驱逐。
CHAT_LOCK_MAX_SIZE = 512

# Webhook 异常追踪:连续 N 次错响应 → warning。N 个错后清零。
WEBHOOK_ANOMALY_TTL_SECONDS = 6 * 3600
WEBHOOK_ANOMALY_THRESHOLD = 25

# Reaction 表情(对应飞书 emoji_type)
REACTION_IN_PROGRESS = "Typing"
REACTION_FAILURE = "CrossMark"

# Phase 32:Webhook 安全/限制配置(从 settings.webhook_* 字段读,缺省用这里)
WEBHOOK_DEFAULT_HOST = "0.0.0.0"
WEBHOOK_DEFAULT_PORT = 9000
WEBHOOK_DEFAULT_PATH = "/lark/webhook"
WEBHOOK_MAX_BODY_BYTES = 1 * 1024 * 1024          # 1 MiB
WEBHOOK_BODY_TIMEOUT_SECONDS = 5
WEBHOOK_RATE_LIMIT_MAX = 60                      # 每窗口最大事件数
WEBHOOK_RATE_LIMIT_WINDOW_SECONDS = 60           # 滑动窗口
WEBHOOK_RATE_LIMIT_MAX_KEYS = 1000               # 不同 IP 计数上限

# Phase 32:入站媒体缓存(图片/文件/音频)。目录可被 OPENCLAW_LARK_MEDIA_DIR 覆盖。
LARK_MEDIA_DEFAULT_DIR = "~/.openclaw/lark_media"
LARK_MEDIA_MAX_BYTES = 50 * 1024 * 1024          # 50 MiB 单文件上限
LARK_MEDIA_CACHE_TTL_SECONDS = 7 * 24 * 3600     # 7 天过期

# 入站媒体文件类型(基于 message_type 字段)
LARK_MEDIA_TYPES = {"image", "file", "audio", "media", "video"}

# Phase 33:支持的出站消息类型
# 参考 Hermes Feishu 4499 处的 msg_type 取值;text / post / interactive
# 是最常用的三档;image / file / share_chat 在出库路径里也需要 base64 + 资源上传,
# 本 Phase 不实现完整 image/file 发送(改由 send_typed("image", {"image_key": "..."}) 提供)
_SUPPORTED_OUTBOUND_MSG_TYPES = {"text", "post", "interactive", "image", "file", "share_chat"}

# Phase 33:最大 message 内容长度(超长截断,防飞书 400)
LARK_MAX_MESSAGE_LENGTH = 30_000


def _is_bot_mentioned(mentions: Optional[list], bot_open_id: Optional[str]) -> bool:
    """CH-1:判断飞书事件里是否 @ 了 bot。

    mentions 是 lark_oapi MentionEvent 列表,任一满足:
    - mentioned_type == "bot"(明确是 bot)
    - id.open_id 等于 bot 自己的 open_id
    即认为被 @。
    """
    if not mentions:
        return False
    for m in mentions:
        try:
            if getattr(m, "mentioned_type", None) == "bot":
                return True
            if bot_open_id and getattr(getattr(m, "id", None), "open_id", None) == bot_open_id:
                return True
        except Exception:
            continue
    return False


def _dedup_path(settings: Optional["LarkSettings"] = None) -> Path:
    """获取去重状态持久化路径。

    优先级: ``LarkSettings.dedup_path``(显式设的) >
    ``OPENCLAW_LARK_DEDUP_PATH`` 环境变量 > ``~/.openclaw/lark_seen_message_ids.json``。

    设为 ``""`` 时返回 ``None`` 的语义(走 in-memory)由调用方处理。
    """
    raw: str = ""
    if settings is not None and getattr(settings, "dedup_path", None):
        raw = settings.dedup_path
    if not raw:
        raw = os.environ.get("OPENCLAW_LARK_DEDUP_PATH", "").strip()
    if not raw:
        raw = DEDUP_DEFAULT_PATH
    return Path(raw).expanduser()


def _allowed_users() -> set[str]:
    """从环境变量读 LARK_ALLOWED_USERS(逗号 / 空白分隔),返回 open_id 集合。

    缺省 / 空 → 视为「不限制」(空集表示放行所有),由 ``check_dm_allowed`` 决定。
    """
    raw = os.environ.get("LARK_ALLOWED_USERS", "").strip()
    if not raw:
        return set()
    return {t.strip() for t in raw.replace(",", " ").split() if t.strip()}


def _group_policy() -> str:
    """``LARK_GROUP_POLICY``: open / allowlist / disabled。

    - open: 不限制群消息(但仍受 allowed_users 影响 —— allowlist 不空时只放白名单)
    - allowlist: 只接受 allowed_users 内的 sender
    - disabled: 群消息一律丢弃
    """
    p = os.environ.get("LARK_GROUP_POLICY", "open").strip().lower()
    if p not in {"open", "allowlist", "disabled"}:
        logger.warning("LARK_GROUP_POLICY=%r 不识别,回退到 open", p)
        return "open"
    return p


def _truncate_text(text: str, max_len: int) -> str:
    """Phase 33:超长文本截断 + 后缀,避免飞书 400。"""
    if len(text) <= max_len:
        return text
    # 极端情况 max_len 极小时,截断 suffix 自身
    if max_len <= 5:
        return text[:max_len]
    suffix = "...(truncated)"
    keep = max(0, max_len - len(suffix))
    return text[:keep] + suffix


def _resolve_receive_id(chat_id: str) -> tuple[str, str]:
    """Phase 33:从 session 中的 chat_id 解析 ``(receive_id, receive_id_type)``。

    规则(参考 Hermes 4491-4495):
    - ``feishu_user_id:`` 前缀 → ``(user_id, "user_id")``(走 user_id_type)
    - ``ou_`` 前缀 → ``(open_id, "open_id")``
    - 其他 → ``(chat_id, "chat_id")``(默认群)

    注:union_id / email 类型为 1.0 后续,这里先覆盖最常用的三种。
    """
    if not chat_id:
        return ("", "chat_id")
    if chat_id.startswith("feishu_user_id:"):
        return (chat_id[len("feishu_user_id:"):], "user_id")
    if chat_id.startswith("ou_"):
        return (chat_id, "open_id")
    return (chat_id, "chat_id")


# ─────────────────────── Webhook 异常追踪器(静态/共享) ───────────────────────
# 进程级单例,instance 方法只是薄包装,便于 Webhook 路由未来直接调。
_webhook_anomaly_counts: dict[str, tuple[int, str, float]] = {}
_webhook_anomaly_lock = asyncio.Lock()


def record_webhook_anomaly(remote_ip: str, status: str) -> None:
    """递增指定 IP 的连续错响应计数;每 ``WEBHOOK_ANOMALY_THRESHOLD`` 次打 warning。

    Hermes 对应实现: ``_record_webhook_anomaly``。这里做成模块级函数,供未来
    Webhook 路由 handler 直接调用,无须实例化 LarkChannel。
    """
    now = time.time()
    entry = _webhook_anomaly_counts.get(remote_ip)
    if entry is not None:
        count, _last_status, first_seen = entry
        if now - first_seen < WEBHOOK_ANOMALY_TTL_SECONDS:
            count += 1
            if count % WEBHOOK_ANOMALY_THRESHOLD == 0:
                logger.warning(
                    "[Lark webhook] 异常:%d 次连续错响应 (%s) from %s over last %.0fs",
                    count, status, remote_ip, now - first_seen,
                )
            _webhook_anomaly_counts[remote_ip] = (count, status, first_seen)
            return
    # 首次或 TTL 过期 → 重置
    _webhook_anomaly_counts[remote_ip] = (1, status, now)


def clear_webhook_anomaly(remote_ip: str) -> None:
    """成功后清零指定 IP 的错响应计数。"""
    _webhook_anomaly_counts.pop(remote_ip, None)


def verify_webhook_token(
    provided: Optional[str], expected: Optional[str]
) -> bool:
    """验证飞书 Webhook URL verification token(``type=url_verification`` 阶段)。

    走 ``hmac.compare_digest`` 防时序攻击。任一为空 → 不通过(强制要求配 token)。
    """
    if not provided or not expected:
        return False
    return hmac.compare_digest(str(provided), str(expected))


def verify_webhook_signature(
    *,
    timestamp: Optional[str],
    nonce: Optional[str],
    body_str: str,
    encrypt_key: str,
    provided_signature: Optional[str],
) -> bool:
    """校验飞书 AES 加密回调的 SHA256 签名。

    算法: ``SHA256(timestamp + nonce + encrypt_key + body_str)``,base64 编码。
    任一缺失 → False(强制配 encrypt_key 才走加密回调)。
    """
    if not (timestamp and nonce and encrypt_key and provided_signature):
        return False
    import base64
    import hashlib
    raw = f"{timestamp}{nonce}{encrypt_key}{body_str}"
    digest = base64.b64encode(hashlib.sha256(raw.encode("utf-8")).digest()).decode("ascii")
    return hmac.compare_digest(digest, str(provided_signature))


# ─────────────────────── Phase 32 媒体下载辅助(同步) ───────────────────────

def _stream_to_file_sync(
    target_path_str: str,
    resp: Any,
) -> Optional[int]:
    """在 executor 线程里把 httpx 流响应写到磁盘。

    超 ``LARK_MEDIA_MAX_BYTES`` → 中断并返回 ``None``(调用方视作失败)。
    写完后 ``.tmp → .real`` 原子替换。

    必须是同步函数 — 防止 ASYNC230(异步函数不应同步写文件)。
    """
    from pathlib import Path as _P
    target = _P(target_path_str)
    tmp = target.with_suffix(target.suffix + ".tmp")
    written = 0
    try:
        with open(tmp, "wb") as f:
            for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                written += len(chunk)
                if written > LARK_MEDIA_MAX_BYTES:
                    f.close()
                    tmp.unlink(missing_ok=True)
                    return None
                f.write(chunk)
        tmp.replace(target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return written


try:  # 飞书 SDK 可选依赖
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        P2ImMessageReceiveV1,
    )

    _HAS_LARK = True
except Exception:  # pragma: no cover - 兼容未装 SDK
    lark = None  # type: ignore[assignment]
    _HAS_LARK = False


class LarkChannel(BaseChannel):
    """飞书自建应用消息渠道(长连接)。

    Phase 31 新增能力:
    - 启动期自动 load 持久化去重 set;每次 receive 后 persist
    - 每个 chat_id 配一个 LRU-bounded asyncio.Lock(同 chat 串行)
    - reaction 事件 + card action 事件走与 message 同一 dispatch 管道
    - 入站时按 LARK_ALLOWED_USERS / LARK_GROUP_POLICY 过滤
    - 派发前后通过 on_processing_start / on_processing_complete 钩子管理 Typing/CrossMark reaction
    """

    name = "lark"

    def __init__(self, agent_loop: AgentLoop, settings: LarkSettings) -> None:
        super().__init__(agent_loop)
        self.settings = settings
        self._ws_client: Optional[Any] = None
        self._stopped = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        # session_id → 最近一条 message_id,send() 用它 reply 原消息
        self._last_msg_id: dict[str, str] = {}
        # CH-1:bot_open_id 缓存槽位(per-instance)。启动期填,运行时只读。
        self._bot_open_id: Any = _UNSET
        # 单实例内的协程锁:多协程同时首次调用时只让一个真的发请求,其他 await 同一个结果。
        self._bot_open_id_lock = asyncio.Lock()
        # Phase 31:per-chat 串行锁池(LRU bounded)
        self._chat_locks: "collections.OrderedDict[str, asyncio.Lock]" = collections.OrderedDict()
        # Phase 31:去重 set(message_id → seen_at timestamp)+ 持久化路径
        self._seen_message_ids: dict[str, float] = {}
        self._seen_message_order: list[str] = []
        # 优先 LarkSettings.dedup_path(可空字符串 → in-memory),再 env,再默认。
        sp = getattr(settings, "dedup_path", None) if settings is not None else None
        if sp == "":
            # 显式空字符串:完全 in-memory,不读不写
            self._dedup_path: Optional[Path] = None
        else:
            self._dedup_path = _dedup_path(settings)
        self._dedup_lock = asyncio.Lock()  # 协程层;json 落盘走 run_in_executor
        if self._dedup_path is not None:
            self._load_seen_message_ids()
        # Phase 31:allowlist 缓存(启动时读一次,env 改了需重启)
        self._allowed_users: set[str] = _allowed_users()
        self._group_policy: str = _group_policy()
        if self._allowed_users:
            logger.info("Lark 启动:LARK_ALLOWED_USERS=%d 个 open_id", len(self._allowed_users))
        logger.info("Lark 启动:group_policy=%s", self._group_policy)
        # Phase 32:入站媒体缓存目录(空字符串 = 关闭)。
        md = getattr(settings, "media_dir", None) if settings is not None else None
        if md == "":
            self._media_dir: Optional[Path] = None
        else:
            env_md = os.environ.get("OPENCLAW_LARK_MEDIA_DIR", "").strip()
            default_md = env_md or LARK_MEDIA_DEFAULT_DIR
            self._media_dir = Path(md or default_md).expanduser()
            try:
                self._media_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                logger.exception(
                    "Lark 媒体目录创建失败: %s,媒体下载关闭", self._media_dir,
                )
                self._media_dir = None
        # Phase 32:webhook 限流(per-key sliding window)
        self._webhook_rate: dict[str, list[float]] = {}
        # Phase 32:webhook runner 句柄(供 stop() 关闭)
        self._webhook_runner: Optional[Any] = None

    # ---------- 公共接口 ----------

    @property
    def available(self) -> bool:
        return _HAS_LARK and bool(self.settings.app_id) and bool(self.settings.app_secret.get_secret_value())

    async def start(self) -> None:
        if not _HAS_LARK:
            raise RuntimeError(
                "lark-oapi 未安装,无法启动飞书渠道。请先 `pip install lark-oapi`"
            )
        if not self.available:
            raise RuntimeError("飞书凭据未配置 (LARK_APP_ID / LARK_APP_SECRET)")

        if not self.settings.use_ws:
            # Phase 32:Webhook 模式已落地。
            # 必须配 LARK_VERIFICATION_TOKEN(Hermes 也强制,防伪造事件)。
            if not self.settings.verification_token:
                raise RuntimeError(
                    "LARK_VERIFICATION_TOKEN 未配置,拒绝启动 Webhook 模式"
                    "(防未鉴权公开端点被滥用)"
                )
            try:
                import aiohttp  # noqa: F401  # 缺此包就 fail-fast
            except Exception as e:
                raise RuntimeError(
                    f"aiohttp 未安装,无法启用 Webhook 模式:`pip install aiohttp` ({e})"
                ) from e
            # CH-1:启动期先 await 一次 bot_open_id(@ 检测需要)
            try:
                await self._fetch_bot_open_id()
            except RuntimeError as e:
                logger.warning("Lark 启动:bot_open_id 拉取失败(%s), @ 检测回退", e)
            self._task = asyncio.create_task(self._webhook_loop())
            logger.info(
                "Lark Webhook 渠道已启动:host=%s port=%d path=%s",
                self.settings.webhook_host, self.settings.webhook_port,
                self.settings.webhook_path,
            )
            await self._stopped.wait()
            return

        # CH-1:启动期先 await 一次 bot_open_id(同步进 WS loop 之前)。
        try:
            await self._fetch_bot_open_id()
        except RuntimeError as e:
            raise RuntimeError(f"启动 Lark 渠道失败:无法获取 bot open_id({e})") from e
        if self._bot_open_id is None:
            logger.warning("Lark 启动:bot_open_id 为空,@ 检测将回退到 mentioned_type")

        # 在后台线程跑 lark WS,避免阻塞 asyncio loop
        self._task = asyncio.create_task(self._ws_loop())
        logger.info("Lark WS 渠道已启动,等待消息...")
        await self._stopped.wait()

    async def stop(self) -> None:
        self._stopped.set()
        if self._ws_client is not None:
            try:
                self._ws_client.stop()
            except Exception:
                pass
        # Phase 32:webhook runner 优雅关闭
        runner = getattr(self, "_webhook_runner", None)
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception:
                logger.exception("Lark webhook runner cleanup 失败")
            self._webhook_runner = None
        # Phase 31:退出时刷一次去重 state
        try:
            await self._persist_seen_message_ids_async()
        except Exception:
            logger.exception("Lark 退出:刷去重状态失败")
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=3)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def send(self, session_id: str, text: str) -> None:
        """主动给 session_id 发送消息(默认 reply 原消息)。

        向后兼容的 plain-text 接口。富文本 / 卡片走 ``send_typed``。
        """
        if not self.available:
            logger.warning("Lark 未配置,无法发送消息")
            return
        if not text:
            return
        message_id = self._last_msg_id.get(session_id, "")
        if not message_id:
            logger.warning(
                "Lark send 失败:session 没有对应 message_id(可能从 WS 收的消息),"
                "session=%s text=%r", session_id, text[:60],
            )
            return
        await self._reply_to_lark(message_id, text)

    # ---------- Phase 33: 通用发送(支持 post / interactive) ----------

    async def send_typed(
        self,
        session_id: str,
        msg_type: str,
        content: Any,
        *,
        reply_to: Optional[str] = None,
    ) -> bool:
        """Phase 33:通用发送,支持 text / post / interactive 等消息类型。

        - ``session_id``:与 dispatch 进来的格式一致 (lark:<chat_id>:<open_id>)
        - ``msg_type``:飞书消息类型(text / post / interactive / image / file ...)
        - ``content``:已序列化的内容(text: dict;text 字段;post: 二维 list;interactive: dict)
        - ``reply_to``:指定要 reply 的 message_id;不传则 create new message
          (由 ``_resolve_receive_id`` 决定 receive_id + receive_id_type)
        - 返回 True/False 表示成功;**不抛**(业务可能只关心是否送达)
        """
        if not self.available:
            logger.warning("Lark 未配置,send_typed 失败")
            return False
        if msg_type not in _SUPPORTED_OUTBOUND_MSG_TYPES:
            logger.error("Lark send_typed 不支持的 msg_type=%s", msg_type)
            return False
        # content 统一成字符串(JSON)
        if isinstance(content, str):
            content_str = content
        else:
            try:
                content_str = json.dumps(content, ensure_ascii=False)
            except (TypeError, ValueError):
                logger.exception("Lark send_typed content 序列化失败")
                return False
        return await self._send_typed(session_id, msg_type, content_str, reply_to=reply_to)

    # ---------- Phase 31 公共钩子(供子类 / 测试 / 外部 handler 复用) ----------

    def _get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        """返回 chat_id 对应的 asyncio.Lock,不存在则创建;LRU bounded。

        活跃锁永不被驱逐(``locked()`` True 时跳过);空间满时优先驱逐空闲锁,
        退路驱逐最旧一个。
        """
        lock = self._chat_locks.get(chat_id)
        if lock is not None:
            self._chat_locks.move_to_end(chat_id)
            return lock
        if len(self._chat_locks) >= CHAT_LOCK_MAX_SIZE:
            evicted = False
            for key in list(self._chat_locks):
                if not self._chat_locks[key].locked():
                    self._chat_locks.pop(key)
                    evicted = True
                    break
            if not evicted:
                self._chat_locks.pop(next(iter(self._chat_locks)))
        lock = asyncio.Lock()
        self._chat_locks[chat_id] = lock
        return lock

    def _check_sender_allowed(self, sender_open_id: str, is_dm: bool) -> tuple[bool, str]:
        """Phase 31:统一 allowlist 闸口。

        返回 ``(allowed, reason)``。DM 与群策略各自独立。
        """
        if is_dm:
            if not self._allowed_users:
                return True, "dm_no_allowlist"
            if sender_open_id in self._allowed_users:
                return True, "dm_in_allowlist"
            return False, "dm_not_in_allowlist"
        # 群消息
        if self._group_policy == "disabled":
            return False, "group_disabled"
        if self._group_policy == "allowlist":
            if not self._allowed_users:
                # 群策略是 allowlist 但 allowlist 空 → 放行(避免误锁)
                logger.warning("Lark 群策略=allowlist 但 LARK_ALLOWED_USERS 为空,放行所有")
                return True, "group_allowlist_empty"
            if sender_open_id in self._allowed_users:
                return True, "group_in_allowlist"
            return False, "group_not_in_allowlist"
        # open
        return True, "group_open"

    async def _is_duplicate(self, message_id: str) -> bool:
        """Phase 31:基于持久化 LRU 的去重,返回 True 表示已见过。

        调用后把新 ID 记入,超过 DEDUP_CACHE_SIZE 就驱逐最旧。落盘异步执行。
        """
        if not message_id:
            return False
        async with self._dedup_lock:
            now = time.time()
            seen_at = self._seen_message_ids.get(message_id)
            if seen_at is not None and (
                seen_at == 0.0  # legacy 0.0 视作 immortal(避免首次升级被全清)
                or DEDUP_TTL_SECONDS <= 0
                or now - seen_at < DEDUP_TTL_SECONDS
            ):
                return True
            self._seen_message_ids[message_id] = now
            self._seen_message_order.append(message_id)
            while len(self._seen_message_order) > DEDUP_CACHE_SIZE:
                stale = self._seen_message_order.pop(0)
                self._seen_message_ids.pop(stale, None)
        # 落盘不持锁(避免 IO 阻塞);失败仅 warning,下次重启可能重复
        try:
            await self._persist_seen_message_ids_async()
        except Exception:
            logger.exception("Lark 持久化去重状态失败")
        return False

    def _load_seen_message_ids(self) -> None:
        """启动时从 JSON 加载历史去重 ID;失败 / 文件不存在 → 当作空。"""
        if self._dedup_path is None:
            return  # in-memory 模式
        try:
            text = self._dedup_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError:
            logger.warning(
                "Lark 去重文件不可读: %s", self._dedup_path, exc_info=True,
            )
            return
        try:
            payload = json.loads(text)
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "Lark 去重文件解析失败: %s", self._dedup_path, exc_info=True,
            )
            return
        seen_data = payload.get("message_ids", {}) if isinstance(payload, dict) else {}
        now = time.time()
        entries: dict[str, float] = {}
        if isinstance(seen_data, list):
            # 兼容旧格式: list[str]
            entries = {str(x).strip(): 0.0 for x in seen_data if str(x).strip()}
        elif isinstance(seen_data, dict):
            for k, v in seen_data.items():
                if not isinstance(k, str) or not k.strip():
                    continue
                try:
                    entries[k] = float(v)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
        # TTL 过滤(ts=0.0 当作永不过期,兼容旧数据)
        valid: dict[str, float] = {
            mid: ts for mid, ts in entries.items()
            if ts == 0.0 or DEDUP_TTL_SECONDS <= 0 or now - ts < DEDUP_TTL_SECONDS
        }
        # 容量裁剪:保留最新的
        sorted_ids = sorted(valid, key=lambda k: valid[k], reverse=True)[:DEDUP_CACHE_SIZE]
        self._seen_message_order = list(reversed(sorted_ids))
        self._seen_message_ids = {k: valid[k] for k in sorted_ids}
        logger.info("Lark 启动:加载去重状态 %d 条 from %s", len(sorted_ids), self._dedup_path)

    async def _persist_seen_message_ids_async(self) -> None:
        """异步落盘去重 state。失败仅 warning。"""
        if self._dedup_path is None:
            return  # in-memory 模式
        loop = asyncio.get_running_loop()
        snapshot = (
            list(self._seen_message_order[-DEDUP_CACHE_SIZE:]),
            {k: self._seen_message_ids[k] for k in self._seen_message_order[-DEDUP_CACHE_SIZE:]
             if k in self._seen_message_ids},
            self._dedup_path,
        )

        def _write() -> None:
            order, mapping, path = snapshot
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"message_ids": mapping}
            # 临时文件 + rename 原子写
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=None),
                encoding="utf-8",
            )
            tmp.replace(path)

        await loop.run_in_executor(None, _write)

    async def _fetch_bot_open_id(self) -> Optional[str]:
        """CH-1:异步拉一次 bot 自己的 open_id,并缓存到 self._bot_open_id。"""
        # 命中缓存(包含 None) → 直接返回。
        cached = self._bot_open_id
        if cached is not _UNSET:
            return None if cached is None else str(cached)

        async with self._bot_open_id_lock:
            cached = self._bot_open_id
            if cached is not _UNSET:
                return None if cached is None else str(cached)

            import httpx

            token = await self._get_tenant_token()
            if not token:
                raise RuntimeError("Lark: 拿不到 tenant_access_token,无法拉 bot open_id")

            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(
                        "https://open.feishu.cn/open-apis/bot/v3/info",
                        headers={"Authorization": f"Bearer {token}"},
                    )
            except Exception as e:
                raise RuntimeError(f"Lark: 拉 bot open_id 网络失败: {e}") from e
            try:
                data = r.json()
            except Exception as e:
                raise RuntimeError(f"Lark: 解析 bot_info 响应失败: {e}") from e

            bot = (data.get("data") or {}).get("bot") or {}
            open_id = bot.get("open_id")
            if open_id:
                self._bot_open_id = open_id
                logger.info("Lark bot open_id 缓存: %s", open_id)
            else:
                self._bot_open_id = None
                logger.warning(
                    "Lark bot open_id 为空(code=%s msg=%s), @ 检测将回退到 mentioned_type",
                    data.get("code"), data.get("msg"),
                )
            return open_id

    # ---------- 内部实现 ----------

    async def _ws_loop(self) -> None:
        """在独立线程跑飞书 WS 客户端,带崩溃重连(REL-1)。"""
        loop = asyncio.get_running_loop()
        backoffs = [1, 2, 4, 8, 16, 30]
        max_attempts = 12
        attempt = 0

        def _on_message(data: Any) -> None:
            try:
                evt = data
                asyncio.run_coroutine_threadsafe(self._handle_event(evt), loop)
            except Exception:
                logger.exception("解析飞书事件失败")

        # Phase 31:把 reaction / card action / 各种 callback 注册到 dispatcher
        # lark-oapi 的 register 接受同步 handler,把数据 dict 塞进我们的 async 管道
        def _on_reaction_created(data: Any) -> None:
            asyncio.run_coroutine_threadsafe(
                self._handle_reaction_event("im.message.reaction.created_v1", data),
                loop,
            )

        def _on_reaction_deleted(data: Any) -> None:
            asyncio.run_coroutine_threadsafe(
                self._handle_reaction_event("im.message.reaction.deleted_v1", data),
                loop,
            )

        def _on_card_action(data: Any) -> None:
            asyncio.run_coroutine_threadsafe(self._handle_card_action(data), loop)

        try:
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
        except Exception:  # pragma: no cover - 异常时 fallback 老 builder
            EventDispatcherHandler = None  # type: ignore[assignment]

        if EventDispatcherHandler is not None:
            try:
                # SDK 0.7+ 新版 builder 接受 encrypt_key / verification_token
                handler = (
                    EventDispatcherHandler.builder(
                        "",
                        self.settings.verification_token.get_secret_value()
                        if self.settings.verification_token else "",
                    )
                    .register_p2_im_message_receive_v1(_on_message)
                    .register_p2_im_message_reaction_created_v1(_on_reaction_created)
                    .register_p2_im_message_reaction_deleted_v1(_on_reaction_deleted)
                    .register_p2_card_action_trigger(_on_card_action)
                    .build()
                )
            except Exception:
                # 老 SDK / 不同签名 → 退到纯 WS builder(只处理 message)
                logger.warning("Lark 高级 event 订阅失败,降级到只收 message 事件", exc_info=True)
                handler = self._build_basic_handler(_on_message)
        else:
            handler = self._build_basic_handler(_on_message)

        while not self._stopped.is_set():
            self._ws_client = lark.ws.Client(
                self.settings.app_id,
                self.settings.app_secret.get_secret_value(),
                event_handler=handler,
                log_level=lark.LogLevel.INFO,
            )
            try:
                await loop.run_in_executor(None, self._ws_client.start)
                if self._stopped.is_set():
                    return
                logger.warning("Lark WS 客户端意外退出,准备重连")
            except Exception:
                logger.exception("Lark WS 崩溃,准备重连")
            finally:
                self._ws_client = None

            attempt += 1
            if attempt > max_attempts:
                logger.error(
                    "Lark WS 重连超上限(%d 次),停止重连(请人工检查凭据 / 网络)",
                    max_attempts,
                )
                return

            delay = backoffs[min(attempt - 1, len(backoffs) - 1)]
            logger.info("Lark WS 第 %d 次重连,等 %ds", attempt, delay)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass

    def _build_basic_handler(self, on_message: Any) -> Any:
        """SDK 不支持 reaction/card 时,只用最基本 message handler。"""
        return (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_message)
            .build()
        )

    # ---------- Phase 32: Webhook 模式(aiohttp) ----------

    async def _webhook_loop(self) -> None:
        """Phase 32:跑 aiohttp app,监听飞书 webhook 回调。

        设计要点(参考 Hermes Feishu 4594-4608):
        - 只用 aiohttp 一个进程内 server,不需要外部 nginx
        - 路由: ``POST {LARK_WEBHOOK_PATH}`` → ``_handle_webhook_request``
        - 关闭:由 ``stop()`` 调 ``runner.cleanup()``
        """
        from aiohttp import web  # type: ignore[import-untyped]

        async def _handler(request: Any) -> Any:
            try:
                return await self._handle_webhook_request(request)
            except Exception:
                logger.exception("Lark webhook 顶层崩溃")
                return web.json_response({"code": 500, "msg": "internal"}, status=500)

        app = web.Application()
        app.router.add_post(self.settings.webhook_path, _handler)
        runner = web.AppRunner(app)
        await runner.setup()
        self._webhook_runner = runner
        site = web.TCPSite(runner, self.settings.webhook_host, self.settings.webhook_port)
        await site.start()
        try:
            await self._stopped.wait()
        finally:
            try:
                await runner.cleanup()
            except Exception:
                pass
            self._webhook_runner = None

    def _check_webhook_rate_limit(self, rate_key: str) -> bool:
        """Phase 32:滑动窗口限流(per key)。

        rate_key 形如 ``{app_id}:{path}:{remote_ip}``,跟 Hermes 对齐。
        返回 True = 放行,False = 已超限。
        """
        now = time.time()
        window = WEBHOOK_RATE_LIMIT_WINDOW_SECONDS
        max_n = WEBHOOK_RATE_LIMIT_MAX
        # 上限保护:超 keys 上限就驱逐最旧
        if len(self._webhook_rate) >= WEBHOOK_RATE_LIMIT_MAX_KEYS:
            oldest_key = next(iter(self._webhook_rate))
            self._webhook_rate.pop(oldest_key, None)
        bucket = self._webhook_rate.get(rate_key, [])
        # 清理窗口外
        cutoff = now - window
        bucket = [t for t in bucket if t >= cutoff]
        if len(bucket) >= max_n:
            self._webhook_rate[rate_key] = bucket
            return False
        bucket.append(now)
        self._webhook_rate[rate_key] = bucket
        return True

    async def _handle_webhook_request(self, request: Any) -> Any:
        """Phase 32:处理一个飞书 webhook 回调。

        守卫顺序(参考 Hermes 3329-3427):
        1. 限流 → 429
        2. Content-Type guard → 415
        3. Content-Length guard → 413
        4. body read with timeout → 408 / 400
        5. JSON 解析 → 400
        6. Verification token → 401-token
        7. URL verification challenge → echo challenge
        8. Signature (when encrypt_key set) → 401-sig
        9. encrypt= → 400
        10. clear_anomaly + dispatch by event_type
        """
        from aiohttp import web  # type: ignore[import-untyped]

        remote_ip = (getattr(request, "remote", None) or "unknown")
        rate_key = f"{self.settings.app_id}:{self.settings.webhook_path}:{remote_ip}"

        # 1. 限流
        if not self._check_webhook_rate_limit(rate_key):
            logger.warning("Lark webhook 限流超限: %s", remote_ip)
            record_webhook_anomaly(remote_ip, "429")
            return web.Response(status=429, text="Too Many Requests")

        # 2. Content-Type guard
        headers = getattr(request, "headers", {}) or {}
        content_type = str(headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
        if content_type and content_type != "application/json":
            logger.warning(
                "Lark webhook Content-Type 拒绝:%r from %s", content_type, remote_ip,
            )
            record_webhook_anomaly(remote_ip, "415")
            return web.Response(status=415, text="Unsupported Media Type")

        # 3. Content-Length guard
        content_length = getattr(request, "content_length", None)
        if content_length is not None and content_length > WEBHOOK_MAX_BODY_BYTES:
            logger.warning(
                "Lark webhook body 过大(Content-Length=%d) from %s",
                content_length, remote_ip,
            )
            record_webhook_anomaly(remote_ip, "413")
            return web.Response(status=413, text="Request body too large")

        # 4. body read
        try:
            body_bytes: bytes = await asyncio.wait_for(
                request.read(),
                timeout=WEBHOOK_BODY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("Lark webhook body 读取超时 from %s", remote_ip)
            record_webhook_anomaly(remote_ip, "408")
            return web.Response(status=408, text="Request Timeout")
        except Exception:
            record_webhook_anomaly(remote_ip, "400")
            return web.json_response({"code": 400, "msg": "failed to read body"}, status=400)

        # 5. 实际 body 大小再次校验(防 Content-Length 撒谎)
        if len(body_bytes) > WEBHOOK_MAX_BODY_BYTES:
            logger.warning(
                "Lark webhook body 实际过大(%d 字节) from %s",
                len(body_bytes), remote_ip,
            )
            record_webhook_anomaly(remote_ip, "413")
            return web.Response(status=413, text="Request body too large")

        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            record_webhook_anomaly(remote_ip, "400")
            return web.json_response({"code": 400, "msg": "invalid json"}, status=400)

        # 6. Verification token
        if self.settings.verification_token:
            header = payload.get("header") or {}
            incoming_token = str(header.get("token") or payload.get("token") or "")
            expected = self.settings.verification_token.get_secret_value()
            if not verify_webhook_token(incoming_token, expected):
                logger.warning("Lark webhook verification token 错 from %s", remote_ip)
                record_webhook_anomaly(remote_ip, "401-token")
                return web.Response(status=401, text="Invalid verification token")

        # 7. URL verification challenge(必须在 token 校验之后!)
        if payload.get("type") == "url_verification":
            return web.json_response({"challenge": payload.get("challenge", "")})

        # 8. Signature(配了 encrypt_key 才强制)
        if self.settings.encrypt_key:
            if not verify_webhook_signature(
                timestamp=str(headers.get("x-lark-request-timestamp", "") or ""),
                nonce=str(headers.get("x-lark-request-nonce", "") or ""),
                body_str=body_bytes.decode("utf-8", errors="replace"),
                encrypt_key=self.settings.encrypt_key.get_secret_value(),
                provided_signature=str(headers.get("x-lark-signature", "") or ""),
            ):
                logger.warning("Lark webhook 签名错 from %s", remote_ip)
                record_webhook_anomaly(remote_ip, "401-sig")
                return web.Response(status=401, text="Invalid signature")

        # 9. encrypt= 暂不支持
        if payload.get("encrypt"):
            record_webhook_anomaly(remote_ip, "400-encrypted")
            return web.json_response(
                {"code": 400, "msg": "encrypted webhook payloads are not supported"},
                status=400,
            )

        # 10. clear anomaly + dispatch
        clear_webhook_anomaly(remote_ip)
        event_type = str((payload.get("header") or {}).get("event_type") or "")
        await self._dispatch_webhook_event(event_type, payload)
        return web.json_response({"code": 0, "msg": "ok"})

    async def _dispatch_webhook_event(self, event_type: str, payload: Any) -> None:
        """Phase 32:把 webhook 事件分发到与 WS 同一管道。

        复用现有 _handle_event / _handle_reaction_event / _handle_card_action,
        payload 已经被解析成 dict,lark-oapi 的 .event.sender/.event.message
        用 SimpleNamespace 包一层即可。
        """
        from types import SimpleNamespace

        def _wrap(obj: Any) -> Any:
            """递归把 dict/list 包装成 SimpleNamespace,叶子保留原值。"""
            if isinstance(obj, dict):
                return SimpleNamespace(**{k: _wrap(v) for k, v in obj.items()})
            if isinstance(obj, list):
                return [_wrap(x) for x in obj]
            return obj

        event = (payload.get("event") or {}) if isinstance(payload, dict) else {}
        data = SimpleNamespace(event=_wrap(event)) if event else SimpleNamespace()

        try:
            if event_type == "im.message.receive_v1":
                await self._handle_event(data)
            elif event_type in {
                "im.message.reaction.created_v1",
                "im.message.reaction.deleted_v1",
            }:
                await self._handle_reaction_event(event_type, data)
            elif event_type == "card.action.trigger":
                await self._handle_card_action(data)
            else:
                logger.debug("Lark webhook 忽略未支持事件类型:%s", event_type or "unknown")
        except Exception:
            logger.exception("Lark webhook 事件分发失败: %s", event_type)

    # ---------- 媒体下载(Phase 32) ----------

    async def _download_inbound_media(
        self,
        message_id: str,
        file_key: str,
        *,
        message_type: str = "file",
    ) -> Optional[Path]:
        """Phase 32:从飞书拉一张入站媒体,落盘到 ``self._media_dir``。

        - 调用 ``im/v1/messages/:message_id/resources/:file_key``(GET)
        - 失败 / 超 LARK_MEDIA_MAX_BYTES / 媒体目录关闭 → 返回 None
        - 文件名 = ``{message_type}_{message_id}_{file_key}.{ext}``
        - 落盘前 SSRF 防御:只有飞书官方域才允许

        幂等:同 (message_id, file_key) 已有缓存 → 直接返回路径。
        """
        if not self._media_dir:
            return None
        if not message_id or not file_key:
            return None
        ext = self._guess_media_ext(message_type, file_key)
        filename = f"{message_type}_{message_id}_{file_key}{ext}"
        # 净化文件名
        filename = "".join(c for c in filename if c.isalnum() or c in "._-") or f"media_{file_key}{ext}"
        target = self._media_dir / filename
        if target.exists() and target.stat().st_size > 0:
            return target

        import httpx

        token = await self._get_tenant_token()
        if not token:
            logger.warning("Lark 媒体下载:拿不到 tenant_access_token")
            return None

        # 飞书资源 API:SSRF 防御只走 open.feishu.cn
        url = (
            f"https://open.feishu.cn/open-apis/im/v1/messages/"
            f"{message_id}/resources/{file_key}"
        )
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                async with client.stream(
                    "GET", url,
                    headers={"Authorization": f"Bearer {token}"},
                ) as resp:
                    if resp.status_code != 200:
                        logger.warning(
                            "Lark 媒体下载失败 http=%d message_id=%s key=%s",
                            resp.status_code, message_id, file_key,
                        )
                        return None
                    # 先读 size header 防超
                    content_length_hdr = resp.headers.get("Content-Length")
                    if content_length_hdr:
                        try:
                            if int(content_length_hdr) > LARK_MEDIA_MAX_BYTES:
                                logger.warning(
                                    "Lark 媒体过大(%s 字节),拒绝下载", content_length_hdr,
                                )
                                return None
                        except ValueError:
                            pass
                    # 流式写 — 文件 IO 走 run_in_executor 避免阻塞事件循环
                    loop = asyncio.get_running_loop()
                    written = await loop.run_in_executor(
                        None, _stream_to_file_sync, str(target), resp,
                    )
                    if written is None:
                        # 中断(超 LARK_MEDIA_MAX_BYTES)
                        return None
            logger.info("Lark 媒体已下载:%s (%d 字节)", target, written)
            return target
        except Exception:
            logger.exception(
                "Lark 媒体下载异常 message_id=%s key=%s", message_id, file_key,
            )
            return None

    @staticmethod
    def _guess_media_ext(message_type: str, file_key: str) -> str:
        """从 message_type 猜扩展名;file_key 结尾有 '.' 也直接取。"""
        if "." in file_key:
            ext = "." + file_key.rsplit(".", 1)[-1].lower()
            if len(ext) <= 6 and ext.replace(".", "").isalnum():
                return ext
        return {
            "image": ".jpg",
            "audio": ".mp3",
            "video": ".mp4",
            "media": ".bin",
        }.get(message_type, ".bin")

    @staticmethod
    def _extract_file_key(msg: Any) -> str:
        """从飞书消息结构里抽 file_key / image_key 等。

        ``content`` 是 ``{"image_key": "..."}`` / ``{"file_key": "...", "file_name": "..."}`` 形式。
        返回空串表示没有可下载的 key。
        """
        try:
            content = json.loads(getattr(msg, "content", "") or "{}")
        except (json.JSONDecodeError, TypeError):
            return ""
        for k in ("image_key", "file_key", "video_key", "audio_key", "media_key"):
            v = content.get(k)
            if isinstance(v, str) and v:
                return v
        return ""

    async def _handle_event(self, evt: Any) -> None:
        """处理一条飞书消息事件。Phase 31:加 allowlist / dedup / per-chat 锁 / reaction 钩子。"""
        try:
            sender = evt.event.sender
            msg = evt.event.message
            chat_id = msg.chat_id
            open_id = sender.sender_id.open_id if sender and sender.sender_id else "unknown"
            message_id = getattr(msg, "message_id", "") or ""
            # Phase 31 顺序:dedup 在最前(同 ID 不论 sender 都丢)
            if message_id and await self._is_duplicate(message_id):
                logger.info("Lark 重复消息丢弃 message_id=%s", message_id)
                return
            text = self._extract_text(msg)
            is_dm = (getattr(msg, "chat_type", "") == "p2p")
            allowed, reason = self._check_sender_allowed(open_id, is_dm=is_dm)
            if not allowed:
                logger.info(
                    "Lark 消息被 allowlist 拒绝 session=%s reason=%s",
                    f"lark:{chat_id}:{open_id}", reason,
                )
                return
            # Phase 32:媒体类型 → 走 _download_inbound_media,把本地路径
            # 塞 metadata,text 改占位符(避免空文本被早丢)。
            # 即便下载失败,有 image_key/file_key 仍生成占位符让 agent 知晓。
            media_paths: list[Path] = []
            media_placeholders: list[str] = []
            msg_type = getattr(msg, "message_type", "") or ""
            if msg_type in LARK_MEDIA_TYPES:
                file_key = self._extract_file_key(msg)
                if file_key:
                    local = await self._download_inbound_media(
                        message_id, file_key, message_type=msg_type,
                    )
                    placeholder = f"[{msg_type}:{file_key}]"
                    if local is not None:
                        media_paths.append(local)
                    media_placeholders.append(placeholder)
            # 媒体占位符拼到 text(空 text 时用占位符;有 text 时追加)
            if media_placeholders:
                if text:
                    text = text + "\n" + "\n".join(media_placeholders)
                else:
                    text = "\n".join(media_placeholders)
            if not text:
                return

            from openclaw.channels.base import IncomingMessage
            session_id = f"lark:{chat_id}:{open_id}"
            if message_id:
                self._last_msg_id[session_id] = message_id
            # CH-1:解析 mentions;若 mention 列表里有 bot 自己的 open_id 则 mentioned=True
            if self._bot_open_id is _UNSET:
                try:
                    bot_open_id: Optional[str] = await self._fetch_bot_open_id()
                except RuntimeError as e:
                    logger.warning("运行时拉 bot open_id 失败,@ 检测回退到 mentioned_type: %s", e)
                    bot_open_id = None
            else:
                cached = self._bot_open_id
                bot_open_id = None if cached is None else str(cached)
            mentioned = _is_bot_mentioned(getattr(msg, "mentions", None), bot_open_id)
            # Phase 31:per-chat 串行处理 —— 同一 chat 锁内 dispatch,避免并发乱序
            chat_lock = self._get_chat_lock(chat_id or "<unknown>")
            async with chat_lock:
                # 进入锁后再做处理中 reaction(成功加完才 dispatch;失败 / 异常时换 CrossMark)
                if message_id:
                    await self._add_processing_reaction(message_id, REACTION_IN_PROGRESS)
                try:
                    await self.dispatch(IncomingMessage(
                        channel=self.name,
                        session_id=session_id,
                        user_id=open_id,
                        text=text,
                        raw=msg,
                        metadata={
                            "is_dm": is_dm,
                            "mentioned": mentioned,
                            "chat_id": chat_id,
                            "message_id": message_id,
                            # Phase 32:媒体下载结果(本机绝对路径列表)
                            "media_paths": [str(p) for p in media_paths],
                            "media_type": msg_type if media_paths else None,
                        },
                    ))
                    # 成功 → 移除 Typing reaction
                    if message_id:
                        await self._remove_processing_reaction(message_id, REACTION_IN_PROGRESS)
                except Exception:
                    # 失败 → Typing 换 CrossMark
                    logger.exception("Lark dispatch 失败 chat_id=%s", chat_id)
                    if message_id:
                        await self._replace_processing_reaction(
                            message_id, REACTION_IN_PROGRESS, REACTION_FAILURE,
                        )
                    raise
        except Exception:
            logger.exception("处理飞书事件失败")

    async def _handle_reaction_event(self, event_type: str, data: Any) -> None:
        """Phase 31:reaction 事件 → 合成 text 事件。

        形如 ``reaction:added:Typing`` / ``reaction:removed:CrossMark``,
        让 agent 通过文本感知用户对 bot 消息的表态。
        """
        try:
            # data 可能是 SimpleNamespace(.event 字段),也可能是 dict("event" 键)
            if hasattr(data, "event") and not isinstance(data, dict):
                event = getattr(data, "event", None) or {}
            elif isinstance(data, dict):
                event = data.get("event") or {}
            else:
                event = data or {}
            # 取 reaction_type,允许 lark-oapi 对象 / dict
            reaction_type_obj: Any = None
            if hasattr(event, "reaction_type") and not isinstance(event, dict):
                reaction_type_obj = getattr(event, "reaction_type", None)
            elif isinstance(event, dict):
                reaction_type_obj = event.get("reaction_type")
            emoji_type = "UNKNOWN"
            if reaction_type_obj is not None:
                et: Any = None
                if hasattr(reaction_type_obj, "emoji_type") and not isinstance(reaction_type_obj, dict):
                    et = getattr(reaction_type_obj, "emoji_type", None)
                elif isinstance(reaction_type_obj, dict):
                    et = reaction_type_obj.get("emoji_type")
                # MagicMock 兼容:str 才算合法值
                if isinstance(et, str) and et:
                    emoji_type = et
            action = "added" if event_type.endswith(".created_v1") else "removed"
            synthetic_text = f"reaction:{action}:{emoji_type}"
            # 取 operator_id.open_id
            operator: Any = None
            if hasattr(event, "operator") and not isinstance(event, dict):
                operator = getattr(event, "operator", None)
            elif isinstance(event, dict):
                operator = event.get("operator")
            open_id = "unknown"
            if operator is not None:
                op_id: Any = None
                if hasattr(operator, "operator_id") and not isinstance(operator, dict):
                    op_id = getattr(operator, "operator_id", None)
                elif isinstance(operator, dict):
                    op_id = operator.get("operator_id")
                if op_id is not None:
                    if hasattr(op_id, "open_id") and not isinstance(op_id, dict):
                        oid = getattr(op_id, "open_id", None)
                        if isinstance(oid, str) and oid:
                            open_id = oid
                    elif isinstance(op_id, dict):
                        oid = op_id.get("open_id")
                        if isinstance(oid, str) and oid:
                            open_id = oid
            # 取 message_id
            message_id: Any = None
            if hasattr(event, "message_id") and not isinstance(event, dict):
                message_id = getattr(event, "message_id", None)
            elif isinstance(event, dict):
                message_id = event.get("message_id")
            if message_id is None:
                message_id = "unknown"
            chat_id = ""
            if hasattr(event, "chat_id") and not isinstance(event, dict):
                cid = getattr(event, "chat_id", None)
                if isinstance(cid, str):
                    chat_id = cid
            elif isinstance(event, dict):
                cid = event.get("chat_id")
                if isinstance(cid, str):
                    chat_id = cid
            from openclaw.channels.base import IncomingMessage
            session_id = f"lark:{chat_id}:{open_id}"
            logger.info(
                "Lark reaction %s %s on message %s → synthetic text",
                action, emoji_type, message_id,
            )
            await self.dispatch(IncomingMessage(
                channel=self.name,
                session_id=session_id,
                user_id=open_id,
                text=synthetic_text,
                raw=data,
                metadata={
                    "is_dm": False,
                    "is_reaction": True,
                    "reaction_action": action,
                    "reaction_emoji": emoji_type,
                    "message_id": str(message_id),
                },
            ))
        except Exception:
            logger.exception("Lark reaction 事件处理失败")

    async def _handle_card_action(self, data: Any) -> None:
        """Phase 31:卡片按钮点击 → 合成 COMMAND 事件。

        形如 ``/card <action_tag>`` —— agent 可用 ``/`` 触发普通命令分支。
        """
        try:
            # data 可能是 SimpleNamespace(.event 字段),也可能是 dict("event" 键)
            if hasattr(data, "event") and not isinstance(data, dict):
                event = getattr(data, "event", None) or {}
            elif isinstance(data, dict):
                event = data.get("event") or {}
            else:
                event = data or {}
            value: Any = None
            tag: Any = None
            if hasattr(event, "action") and not isinstance(event, dict):
                action = getattr(event, "action", None)
                if action is not None:
                    value = getattr(action, "value", None)
                    tag = getattr(action, "tag", None)
            elif isinstance(event, dict):
                action = event.get("action")
                if isinstance(action, dict):
                    value = action.get("value")
                    tag = action.get("tag")
            # 取 str 形式:优先 value,其次 tag,最后 unknown(避免 MagicMock repr)
            if isinstance(value, str) and value:
                action_tag = value
            elif isinstance(tag, str) and tag:
                action_tag = tag
            else:
                action_tag = "unknown"
            operator: Any = None
            if hasattr(event, "operator") and not isinstance(event, dict):
                operator = getattr(event, "operator", None)
            elif isinstance(event, dict):
                operator = event.get("operator")
            open_id = "unknown"
            if operator is not None:
                if hasattr(operator, "open_id") and not isinstance(operator, dict):
                    oid = getattr(operator, "open_id", None)
                    if isinstance(oid, str) and oid:
                        open_id = oid
                elif isinstance(operator, dict):
                    oid = operator.get("open_id")
                    if isinstance(oid, str) and oid:
                        open_id = oid
            chat_id = ""
            if hasattr(event, "chat_id") and not isinstance(event, dict):
                cid = getattr(event, "chat_id", None)
                if isinstance(cid, str):
                    chat_id = cid
            elif isinstance(event, dict):
                cid = event.get("chat_id")
                if isinstance(cid, str):
                    chat_id = cid
            from openclaw.channels.base import IncomingMessage
            session_id = f"lark:{chat_id}:{open_id}"
            synthetic_text = f"/card {action_tag}"
            logger.info(
                "Lark card action %r from %s in %s → synthetic COMMAND",
                action_tag, open_id, chat_id,
            )
            await self.dispatch(IncomingMessage(
                channel=self.name,
                session_id=session_id,
                user_id=open_id,
                text=synthetic_text,
                raw=data,
                metadata={
                    "is_dm": False,
                    "is_card_action": True,
                    "card_action": action_tag,
                    "chat_id": chat_id,
                },
            ))
        except Exception:
            logger.exception("Lark card action 事件处理失败")

    # ---------- Processing reactions(可选,失败降级为 no-op) ----------

    async def _add_processing_reaction(self, message_id: str, emoji_type: str) -> None:
        """Phase 31:加 Typing reaction;失败仅 warning(不阻断主流程)。"""
        if not _HAS_LARK or not message_id:
            return
        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageReactionRequest,
                CreateMessageReactionRequestBody,
            )
            client = lark.Client.builder() \
                .app_id(self.settings.app_id) \
                .app_secret(self.settings.app_secret.get_secret_value()) \
                .build()
            body = (
                CreateMessageReactionRequestBody.builder()
                .reaction_type({"emoji_type": emoji_type})
                .build()
            )
            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(body)
                .build()
            )

            def _call() -> Any:
                return client.im.v1.message_reaction.create(request)

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _call)
        except Exception:
            logger.warning("Lark add reaction %s 失败", emoji_type, exc_info=True)

    async def _remove_processing_reaction(self, message_id: str, emoji_type: str) -> None:
        """Phase 31:移除 reaction;失败仅 warning。"""
        if not _HAS_LARK or not message_id:
            return
        try:
            from lark_oapi.api.im.v1 import DeleteMessageReactionRequest
            client = lark.Client.builder() \
                .app_id(self.settings.app_id) \
                .app_secret(self.settings.app_secret.get_secret_value()) \
                .build()
            request = (
                DeleteMessageReactionRequest.builder()
                .message_id(message_id)
                .reaction_id(emoji_type)  # 简化:实际上要 reaction_id
                .build()
            )

            def _call() -> Any:
                return client.im.v1.message_reaction.delete(request)

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _call)
        except Exception:
            logger.warning("Lark remove reaction %s 失败", emoji_type, exc_info=True)

    async def _replace_processing_reaction(
        self, message_id: str, _old: str, new: str
    ) -> None:
        """失败时换 CrossMark(简化版:直接 add new;SDK 拿 reaction_id 流程较繁琐,这里弱化处理)。"""
        if not _HAS_LARK or not message_id:
            return
        try:
            await self._add_processing_reaction(message_id, new)
        except Exception:
            pass

    @staticmethod
    def _extract_text(msg: Any) -> str:
        """从飞书消息结构里提取纯文本,支持 text 和 post 类型。

        Phase 31 增强:
        - text 类型:直取
        - post 类型:遍历 [[seg, ...], ...] 行,只收 tag==text 的段,遇到 at
          段用 mentions 里的 name 替换占位(若有)
        - 其它(image / file / audio / media):返回空(Phase 31 暂不下载,
          留给后续 Phase;P31-H 留 hookable 接口)
        """
        try:
            content = json.loads(msg.content or "{}")
        except json.JSONDecodeError:
            return ""
        if msg.message_type == "text":
            return (content.get("text") or "").strip()
        if msg.message_type == "post":
            # post 形如: {"content": [[{tag,text/lang/...}], ...]}
            # 第二层 list 是行,行内是段
            post = content.get("content") or [[]]
            mentions = getattr(msg, "mentions", None) or []
            # mentions 是 lark-oapi 对象列表,每个有 .key (at 时的占位如 "@_user_1")
            # 与 .id.open_id / .name / .id.name
            mention_by_key: dict[str, str] = {}
            for m in mentions:
                key = getattr(m, "key", None)
                name = (
                    getattr(m, "name", None)
                    or getattr(getattr(m, "id", None), "name", None)
                    or ""
                )
                if key and name:
                    mention_by_key[str(key)] = str(name)
            lines: list[str] = []
            for line in post:
                if not isinstance(line, list):
                    continue
                seg_text: list[str] = []
                for seg in line:
                    if not isinstance(seg, dict):
                        continue
                    tag = seg.get("tag")
                    if tag == "text":
                        seg_text.append(str(seg.get("text") or ""))
                    elif tag == "at":
                        # 占位符在纯文本里形如 "@_user_1"
                        # mention_by_key miss 时回退成 "@_user"(不要再补 @)
                        seg_text.append(mention_by_key.get(seg.get("user_id", ""), "@_user"))
                joined = "".join(seg_text).strip()
                if joined:
                    lines.append(joined)
            return "\n".join(lines)
        return ""

    async def _reply_to_lark(self, message_id: str, text: str) -> None:
        """回复一条飞书消息(用 im/v1/messages/:id/reply)。

        Phase 33:内容超 ``LARK_MAX_MESSAGE_LENGTH`` 时截断,避免飞书 400。
        """
        import httpx

        if not text:
            return
        text = _truncate_text(text, LARK_MAX_MESSAGE_LENGTH)

        token = await self._get_tenant_token()
        if not token:
            logger.error("reply 失败:拿不到 tenant_access_token")
            return

        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
        body = {
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, json=body, headers=headers)
                ctype = getattr(r, "headers", {}).get("content-type", "")
                if ctype.startswith("application/json"):
                    try:
                        data = r.json()
                    except Exception:
                        data = {}
                else:
                    data = {}
                if r.status_code != 200 or data.get("code", 0) != 0:
                    logger.error(
                        "reply 失败 http=%s code=%s msg=%s body=%s",
                        r.status_code, data.get("code"), data.get("msg"), data,
                    )
                else:
                    logger.info("reply 成功 message_id=%s len=%d", message_id, len(text))
        except Exception:
            logger.exception("回复飞书消息失败")

    async def _get_tenant_token(self) -> str | None:
        import httpx

        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        body = {
            "app_id": self.settings.app_id,
            "app_secret": self.settings.app_secret.get_secret_value(),
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(url, json=body)
                r.raise_for_status()
                data = r.json()
                return data.get("tenant_access_token")
        except Exception:
            logger.exception("获取 tenant_access_token 失败")
            return None

    # ---------- Phase 33: 通用 send_typed / _post_message / builders ----------

    async def _send_typed(
        self,
        session_id: str,
        msg_type: str,
        content_str: str,
        *,
        reply_to: Optional[str] = None,
    ) -> bool:
        """Phase 33:内部 send_typed 真正发出去。返回 True/False,失败仅 logger.error。"""
        import httpx

        token = await self._get_tenant_token()
        if not token:
            logger.error("Lark send_typed:拿不到 tenant_access_token")
            return False

        # 从 session_id 解析 chat_id:形如 lark:<chat_id>:<open_id>
        # chat_id 可能含 ":"(如 feishu_user_id:u_abc),所以应取 last-1 而不是 parts[1]。
        # 形如: lark / <chat_id 全段> / <open_id>
        parts = session_id.split(":")
        if len(parts) >= 3:
            # 拼接中间所有段为 chat_id
            chat_id = ":".join(parts[1:-1])
        elif len(parts) == 2:
            chat_id = parts[1]
        else:
            chat_id = ""
        receive_id, receive_id_type = _resolve_receive_id(chat_id)

        if reply_to:
            # 走 reply API
            url = f"https://open.feishu.cn/open-apis/im/v1/messages/{reply_to}/reply"
            body = {"msg_type": msg_type, "content": content_str}
        else:
            # 走 create message API
            if not receive_id:
                logger.error(
                    "Lark send_typed:session_id 解析不到 receive_id session=%s",
                    session_id,
                )
                return False
            url = "https://open.feishu.cn/open-apis/im/v1/messages"
            body = {
                "msg_type": msg_type,
                "content": content_str,
                "receive_id": receive_id,
            }
            # 通过 query string 传 receive_id_type(reply API 不需要)
            url = f"{url}?receive_id_type={receive_id_type}"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, json=body, headers=headers)
                ctype = getattr(r, "headers", {}).get("content-type", "")
                data: dict[str, Any] = {}
                if ctype.startswith("application/json"):
                    try:
                        data = r.json()
                    except Exception:
                        data = {}
                if r.status_code != 200 or data.get("code", 0) != 0:
                    logger.error(
                        "Lark send_typed 失败 http=%s code=%s msg=%s body=%s",
                        r.status_code, data.get("code"), data.get("msg"), data,
                    )
                    return False
                logger.info(
                    "Lark send_typed 成功 session=%s msg_type=%s",
                    session_id, msg_type,
                )
                return True
        except Exception:
            logger.exception("Lark send_typed 异常")
            return False

    async def _post_message(
        self,
        receive_id: str,
        receive_id_type: str,
        msg_type: str,
        content_str: str,
    ) -> bool:
        """Phase 33:直接发到指定 receive_id 的低层入口。

        不基于 session_id 解析;由调用方(比如 send_typed / 测试)自己提供。
        """
        return await self._send_typed(
            f"lark:{receive_id}:manual",
            msg_type,
            content_str,
        )

    # ---------- Phase 33: 出站消息 builder helpers ----------

    @staticmethod
    def build_text_payload(text: str) -> dict[str, str]:
        """Phase 33:text 类型 payload 助手(对齐 ``msg_type=text`` 约定)。"""
        return {"text": text}

    @staticmethod
    def build_post_payload(
        lines: list[list[dict[str, Any]]],
        *,
        title: Optional[str] = None,
    ) -> dict[str, Any]:
        """Phase 33:post 类型 payload builder。

        ``lines`` 是 ``[[{"tag": "text", "text": "..."}], ...]`` 二维结构;
        ``title`` 是可选的标题(空字符串也行,飞书会渲染在最上方)。
        """
        payload: dict[str, Any] = {"content": lines}
        if title:
            payload["title"] = title
        return payload

    @staticmethod
    def build_at_post_payload(
        open_ids: list[str],
        text: str = "",
        *,
        title: Optional[str] = None,
    ) -> dict[str, Any]:
        """Phase 33:@ 多人 + 文本的 post 助手(常见\"@张三 @李四 看看\"用法)。"""
        line: list[dict[str, Any]] = [
            {"tag": "at", "user_id": oid} for oid in open_ids if oid
        ]
        if text:
            line.append({"tag": "text", "text": text})
        return LarkChannel.build_post_payload([line], title=title)

    @staticmethod
    def build_interactive_card_payload(
        *,
        elements: list[dict[str, Any]],
        header: Optional[dict[str, Any]] = None,
        config: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Phase 33:interactive (卡片) 消息 payload builder。

        ``elements`` 是 div / action 等节点列表;
        ``header`` 是可选的标题 / 颜色(``{"title": "...", "template": "blue"}``);
        ``config`` 是 ``{"wide_screen_mode": true, ...}`` 类选项。
        """
        payload: dict[str, Any] = {"elements": elements}
        if header is not None:
            payload["header"] = header
        if config is not None:
            payload["config"] = config
        return payload
