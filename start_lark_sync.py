#!/usr/bin/env python3
"""OpenClaw 飞书渠道同步入口。

与 examples/lark_run.py 风格一致 (直接用 lark.ws.Client + EventDispatcherHandler),
不依赖 openclaw.channels.lark.LarkChannel (后者走 async 路径, 与 lark-oapi 内部 event loop 冲突)。

行为:
1. WS 收到 im.message.receive_v1 → 立即给用户消息加 🤔 reaction (秒级反馈, 无文字占位)
2. 按 open_id 加载该用户最近 HISTORY_WINDOW 条对话历史 (含位置等关键信息)
3. 调 LLM (AGNES_API_KEY) 拿回复; system prompt 注入该用户的"已记住信息"
4. 直接 reply 一条 text 消息; 把 user/assistant 两条都写入 history
5. 静默注册 message_read_v1 / reaction_* 事件, 避免 SDK 抛 "processor not found"
"""
import os
import sys
import json
import re
import time
import threading
from collections import defaultdict, deque
from pathlib import Path

# 强制 UTF-8 输出 (Windows GBK 控制台会乱码, Start-Process 重定向到 .log 也是 GBK)
# PYTHONIOENCODING 必须在 Python 启动前环境变量就有, 这里 setdefault 已晚。
# 因此启动时必须加 -X utf8 参数。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Windows 兼容: 用 msvcrt.locking 替代 fcntl.flock (单实例文件锁)
if sys.platform == "win32":
    import msvcrt

    class _FileLock:
        def __init__(self, path: Path):
            self.path = path
            self.fp = None

        def acquire(self) -> bool:
            self.fp = open(self.path, "w")
            try:
                msvcrt.locking(self.fp.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except (OSError, IOError):
                return False

        def release(self) -> None:
            try:
                msvcrt.locking(self.fp.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
            self.fp.close()
else:
    import fcntl

    class _FileLock:
        def __init__(self, path: Path):
            self.path = path
            self.fp = None

        def acquire(self) -> bool:
            self.fp = open(self.path, "w")
            try:
                fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except (BlockingIOError, OSError):
                return False

        def release(self) -> None:
            try:
                fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            self.fp.close()

# 确保从项目根目录加载
project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

# 加载环境变量
from dotenv import load_dotenv
load_dotenv(project_root / ".env")

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from openclaw.core.config import ConfigLoader
from openclaw.core.logging import get_logger

logger = get_logger(__name__)

# === 简易会话记忆 (in-memory, 单进程内有效) ===
# 生产环境应该用 Redis / 长期记忆, 这里先满足"不要失忆"的基本要求。
HISTORY_WINDOW = 20  # 每个用户最多保留 N 条
_MAX_HISTORY = {"default": HISTORY_WINDOW}

# open_id -> deque([{"role": ..., "content": ...}, ...])
_histories: dict[str, "deque[dict]"] = defaultdict(lambda: deque(maxlen=HISTORY_WINDOW))
# open_id -> {key: value}  (例: {"location": "深圳市盐田区"})
_memories: dict[str, dict[str, str]] = defaultdict(dict)
_history_lock = threading.Lock()

SYSTEM_PROMPT_BASE = (
    "你是 Claw, 一只本地龙虾做的 AI 助理。简洁高效, 优先用工具拿真实数据, "
    "回答用中文, 不确定就直说。"
)

# 简单正则提取"已记住"的事实
_LOCATION_RE = re.compile(
    r"(?:我|本人|在|位于|住|生活|工作)?\s*(?:是|在)?\s*"
    r"(?P<loc>([\u4e00-\u9fa5]{2,}(?:省|自治区|特别行政区)?)"
    r"(?:[\u4e00-\u9fa5]{0,8}市)?"
    r"(?:[\u4e00-\u9fa5]{0,8}区|县|旗))",
)
# 显式"请记住"触发
_REMEMBER_RE = re.compile(r"请记住|记住(?:我)?|remember|记住一下")


def _extract_memory(user_id: str, text: str) -> None:
    """从用户消息提取需要记住的信息, 写入 _memories。"""
    if _REMEMBER_RE.search(text):
        m = _LOCATION_RE.search(text)
        if m:
            _memories[user_id]["location"] = m.group("loc")
            logger.info("[Memory] user=%s 记住 location=%s", user_id, m.group("loc"))


def _build_system_prompt(user_id: str) -> str:
    """根据已记住的事实拼 system prompt。"""
    mem = _memories.get(user_id, {})
    if not mem:
        return SYSTEM_PROMPT_BASE
    facts = "; ".join(f"{k}={v}" for k, v in mem.items())
    return (
        f"{SYSTEM_PROMPT_BASE}\n\n"
        f"你与该用户的对话上下文已知以下事实 (用户明确要求记住): {facts}。\n"
        f"这些信息会在多轮对话中保持, 不要重复询问。"
    )


def _extract_text(msg) -> str:
    """从飞书 message 提取纯文本。"""
    try:
        content_obj = json.loads(msg.content)
        return content_obj.get("text", "")
    except Exception:
        return msg.content


def _on_message(data):
    """SDK 传入 P2ImMessageReceiveV1 对象。"""
    if not isinstance(data, P2ImMessageReceiveV1):
        logger.warning("非预期 data type: %s", type(data).__name__)
        return

    try:
        msg = data.event.message
        chat_id = msg.chat_id
        message_id = msg.message_id
        text = _extract_text(msg)
        if not text:
            return

        sender = data.event.sender
        open_id = sender.sender_id.open_id if sender and sender.sender_id else "unknown"

        logger.info("[Lark] 收到消息: chat=%s open=%s text=%r", chat_id, open_id, text)

        # 0) 提取记忆 (即使最终没调 LLM 也要更新, 防止时序问题)
        with _history_lock:
            _extract_memory(open_id, text)
            history = list(_histories[open_id])  # snapshot
            system_prompt = _build_system_prompt(open_id)

        import httpx

        # 1) tenant token
        r_tok = httpx.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": os.environ["LARK_APP_ID"], "app_secret": os.environ["LARK_APP_SECRET"]},
            timeout=10,
        )
        token = r_tok.json().get("tenant_access_token")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # 2) reaction
        r_react = httpx.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions",
            headers=headers,
            json={"reaction_type": {"emoji_type": "THINKING"}},
            timeout=10,
        )
        if r_react.status_code == 200 and r_react.json().get("code") == 0:
            logger.info("[Lark] 已加 🤔 reaction: %s", r_react.json().get("data", {}).get("reaction_id"))
        else:
            logger.warning("[Lark] reaction 失败: %s", r_react.text[:200])

        # 3) 调 LLM (注入 history + system 记忆)
        # 加重试: agnes-ai 偶发 ReadTimeout, 第一次 30s 不行就再来一次 (60s), 还不行就认了
        api_key = os.environ.get("AGNES_API_KEY", "")
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": text})

        reply = None
        for attempt, timeout in enumerate([30, 60, 90], start=1):
            try:
                r_llm = httpx.post(
                    "https://apihub.agnes-ai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": "agnes-2.0-flash",
                        "messages": messages,
                        "max_tokens": 500,
                    },
                    timeout=timeout,
                )
                if r_llm.status_code == 200:
                    reply = r_llm.json()["choices"][0]["message"]["content"]
                    if attempt > 1:
                        logger.info("[Lark] LLM 第 %d 次尝试成功", attempt)
                    break
                else:
                    logger.warning("[Lark] LLM HTTP %d, 重试中", r_llm.status_code)
            except httpx.ReadTimeout:
                logger.warning("[Lark] LLM ReadTimeout (attempt=%d, timeout=%ds), 重试中", attempt, timeout)
            except Exception as e:
                logger.warning("[Lark] LLM 异常 %s, 重试中", e)

        if reply is None:
            reply = "LLM 调用连续失败, 请稍后再试。"
        logger.info("[Lark] LLM 回复: %r", reply[:200])

        # 4) 写回 history (user + assistant)
        with _history_lock:
            _histories[open_id].append({"role": "user", "content": text})
            _histories[open_id].append({"role": "assistant", "content": reply})

        # 5) reply 最终回复
        r_reply = httpx.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
            headers=headers,
            json={"msg_type": "text", "content": json.dumps({"text": reply}, ensure_ascii=False)},
            timeout=10,
        )
        logger.info("[Lark] reply 发送状态: %d", r_reply.status_code)
    except Exception:
        logger.exception("处理消息失败")


# 加载配置 (校验用)
config_path = project_root / "openclaw.yaml"
ConfigLoader(config_path).load()

# === 单实例锁 ===
# 防止 Trae IDE 等启动器把同一脚本跑出多份 (每个都连飞书 WS, 飞书随机分配消息,
# 会导致 history 失忆 / 反应不一致 / 重复回复)。
# 用文件锁 + 短暂轮询: 等待 5 秒让先启动的进程先抢到, 抢不到就让出, 避免 race。
_lock_path = project_root / ".start_lark_sync.lock"
_lock = _FileLock(_lock_path)
_acquired = False
for _ in range(20):  # 总共等 5s, 200ms 一次
    if _lock.acquire():
        _acquired = True
        break
    time.sleep(0.25)
if not _acquired:
    logger.error("另一个 start_lark_sync 进程已在运行 (lock=%s), 退出避免重复消费消息。", _lock_path)
    sys.exit(0)
_lock.fp.write(f"{os.getpid()}\n")
_lock.fp.flush()
logger.info("获取单实例锁 (pid=%d)", os.getpid())

# 启动 WS
handler = (
    lark.EventDispatcherHandler.builder("", "")
    .register_p2_im_message_receive_v1(_on_message)
    # 静默处理其他已订阅事件, 避免 SDK 抛 "processor not found"
    .register_p2_im_message_message_read_v1(lambda data: None)
    .register_p2_im_message_reaction_created_v1(lambda data: None)
    .register_p2_im_message_reaction_deleted_v1(lambda data: None)
    .build()
)

client = lark.ws.Client(
    os.environ["LARK_APP_ID"],
    os.environ["LARK_APP_SECRET"],
    event_handler=handler,
    log_level=lark.LogLevel.INFO,
)

logger.info("启动 WS 客户端 (同步模式, 记忆窗口=%d)...", HISTORY_WINDOW)
try:
    client.start()
except KeyboardInterrupt:
    logger.info("已停止")
