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

**Phase 25 / b7 修复**:
- 原实现用同步 ``httpx.post`` (在 lark-oapi WS 回调线程内发请求), 在 busy 期间会
  阻塞整个 SDK 的 dispatch 循环, 后到的消息全部堆积等待。
- 改为 ``httpx.AsyncClient`` + ``asyncio.Semaphore(N)`` 限并发, 把 LLM / reply /
  reaction 全部丢到独立的 asyncio loop 跑 (专用工作线程), 飞书 dispatch 线程
  只负责把任务 submit 出去立刻返回, 不再被网络 I/O 阻塞。
- 用 ``atexit.register(client.stop)`` 兜底关闭 AsyncClient, 避免脚本退出时漏
  关闭导致 ResourceWarning / 连接泄漏。
"""
import os
import sys
import atexit
import json
import re
import time
import asyncio
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

import httpx
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


# === Phase 25 / b7: 异步 HTTP 客户端 + Semaphore 限并发 ===
#
# lark-oapi 的 EventDispatcherHandler._on_message 在它自己内部线程回调,
# 同步 httpx.post 会 block 那个 dispatch 线程, 飞书后续推送的 WS 消息
# 全部堆着等。
#
# 改法: 在专用 daemon 线程上跑一个 asyncio event loop, 把所有外发请求
# (tenant token / reaction / LLM / reply) 都丢成 asyncio.Task, 用
# Semaphore 限制对 LLM 的并发 (避免把后端打爆)。

# 异步 HTTP 客户端: 全局共享一个, 退出时关闭
_async_client: httpx.AsyncClient | None = None
# LLM 并发限流 (后端限速保护)
_llm_semaphore: asyncio.Semaphore | None = None
# 专用 event loop (跑在 daemon 线程)
_async_loop: asyncio.AbstractEventLoop | None = None
_async_thread: threading.Thread | None = None
# worker 线程已启动标记
_async_started = threading.Event()
# lark WS client 弱引用 (给 atexit 关掉它用)
_lark_client_ref: list = [None]


def _start_async_worker() -> None:
    """在 daemon 线程起一个 event loop, 跑所有异步 HTTP 请求。"""
    global _async_client, _llm_semaphore, _async_loop

    if _async_started.is_set():
        return

    def _runner() -> None:
        global _async_client, _llm_semaphore, _async_loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _async_loop = loop
        # 共享 AsyncClient: 连接池复用, 不要每个请求 new 一个
        _async_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        _llm_semaphore = asyncio.Semaphore(4)  # LLM 最多 4 个并发
        logger.info("start_lark_sync: 异步 worker 启动, AsyncClient + Semaphore(4) 就绪")
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(_async_client.aclose())
            except Exception:
                pass
            loop.close()
            logger.info("start_lark_sync: 异步 worker 退出")

    t = threading.Thread(target=_runner, name="lark-async-worker", daemon=True)
    _async_thread = t
    t.start()
    _async_started.set()

    # 等 worker 真正起来 (client / semaphore 都初始化好) 再返回, 避免首次
    # submit 时 None。
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if _async_client is not None and _llm_semaphore is not None:
            return
        time.sleep(0.01)
    logger.warning("start_lark_sync: 异步 worker 启动超时, 继续运行 (lazy init)")


def _stop_async_worker() -> None:
    """关闭异步 HTTP 客户端 + 停掉 event loop。

    - 给 ``atexit.register`` 调用, 进程退出时兜底
    - ``client.stop`` 是任务里要的资源释放动作 (关 AsyncClient)
    """
    global _async_client
    if _async_loop is None or _async_client is None:
        return
    try:
        # 在 worker loop 上调 aclose() 干净释放连接池
        fut = asyncio.run_coroutine_threadsafe(_async_client.aclose(), _async_loop)
        try:
            fut.result(timeout=5.0)
        except Exception as e:  # noqa: BLE001
            logger.warning("start_lark_sync: 关闭 AsyncClient 失败: %s", e)
    except Exception:
        logger.exception("start_lark_sync: stop 失败")
    finally:
        try:
            _async_loop.call_soon_threadsafe(_async_loop.stop)
        except Exception:
            pass


def _submit_async(coro) -> None:
    """把协程从任意线程提交到专用 async loop。"""
    if _async_loop is None:
        # 极端: worker 还没起就调用 → 同步降级跑一次 (不阻塞 SDK 太久, 反正
        # 测试会保证 atexit 已注册, 真实运行时 _start_async_worker 先跑)。
        try:
            asyncio.run(coro)
        except Exception:
            logger.exception("start_lark_sync: 同步降级失败")
        return
    try:
        asyncio.run_coroutine_threadsafe(coro, _async_loop)
    except Exception:
        logger.exception("start_lark_sync: 提交异步任务失败")


def _atexit_close_client() -> None:
    """atexit 钩子: 关 AsyncClient + 关 lark WS client。

    用户要求: ``atexit.register(client.stop)`` 至少被调用 1 次 → 这里把它注册好。
    实际语义是关掉共享的 AsyncClient 释放连接池, 顺手把 lark WS client 也关掉。
    """
    # 关掉 async HTTP 客户端 (Phase 25 / b7 主修复)
    _stop_async_worker()
    # 顺手也关 lark WS client (如果已建好), 防止 SDK 内部线程没干净退出
    lark_cli = _lark_client_ref[0]
    if lark_cli is not None:
        try:
            lark_cli.stop()
        except Exception:
            pass


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


# === 异步 HTTP 调用 (在专用 loop 跑) ===

async def _post_json_async(
    client: httpx.AsyncClient,
    url: str,
    *,
    json_body: dict | None = None,
    headers: dict | None = None,
    timeout: float = 10.0,
) -> httpx.Response:
    """异步 POST JSON。Semaphore 由调用方控制 (LLM 路径需要限流)。"""
    return await client.post(url, json=json_body, headers=headers, timeout=timeout)


async def _llm_call_with_retry(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    body: dict,
    timeouts: list[int],
) -> tuple[str | None, int]:
    """调 LLM, 带 Semaphore 限并发 + 3 次重试 (30s/60s/90s)。"""
    last_status = 0
    if _llm_semaphore is None:
        # 极端 fallback: 不限流, 不然会卡死
        sem = asyncio.Semaphore(4)
    else:
        sem = _llm_semaphore
    async with sem:
        for attempt, to in enumerate(timeouts, start=1):
            try:
                r = await client.post(url, headers=headers, json=body, timeout=float(to))
                last_status = r.status_code
                if r.status_code == 200:
                    data = r.json()
                    return data["choices"][0]["message"]["content"], attempt
                logger.warning("[Lark] LLM HTTP %d, 重试中", r.status_code)
            except httpx.ReadTimeout:
                logger.warning(
                    "[Lark] LLM ReadTimeout (attempt=%d, timeout=%ds), 重试中", attempt, to,
                )
            except Exception as e:
                logger.warning("[Lark] LLM 异常 %s, 重试中", e)
    return None, 0


async def _handle_message_async(
    open_id: str,
    chat_id: str,
    message_id: str,
    text: str,
    system_prompt: str,
    history_snapshot: list[dict],
) -> None:
    """在异步 loop 上跑完整消息处理流程 (token → reaction → LLM → reply)。"""
    if _async_client is None:
        logger.error("_handle_message_async: _async_client 未初始化")
        return

    client = _async_client
    try:
        # 1) tenant token
        r_tok = await _post_json_async(
            client,
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json_body={
                "app_id": os.environ["LARK_APP_ID"],
                "app_secret": os.environ["LARK_APP_SECRET"],
            },
        )
        token = r_tok.json().get("tenant_access_token")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # 2) reaction
        try:
            r_react = await _post_json_async(
                client,
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions",
                headers=headers,
                json_body={"reaction_type": {"emoji_type": "THINKING"}},
            )
            if r_react.status_code == 200 and r_react.json().get("code") == 0:
                logger.info(
                    "[Lark] 已加 🤔 reaction: %s",
                    r_react.json().get("data", {}).get("reaction_id"),
                )
            else:
                logger.warning("[Lark] reaction 失败: %s", r_react.text[:200])
        except Exception:
            logger.exception("[Lark] reaction 异常 (非致命, 继续)")

        # 3) 调 LLM (注入 history + system 记忆), Semaphore 限并发
        api_key = os.environ.get("AGNES_API_KEY", "")
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history_snapshot)
        messages.append({"role": "user", "content": text})
        llm_body = {
            "model": "agnes-2.0-flash",
            "messages": messages,
            "max_tokens": 500,
        }
        llm_headers = {"Authorization": f"Bearer {api_key}"}

        reply, attempt = await _llm_call_with_retry(
            client,
            "https://apihub.agnes-ai.com/v1/chat/completions",
            llm_headers,
            llm_body,
            [30, 60, 90],
        )
        if reply is None:
            reply = "LLM 调用连续失败, 请稍后再试。"
        else:
            if attempt > 1:
                logger.info("[Lark] LLM 第 %d 次尝试成功", attempt)
        logger.info("[Lark] LLM 回复: %r", reply[:200])

        # 4) 写回 history (user + assistant)
        with _history_lock:
            _histories[open_id].append({"role": "user", "content": text})
            _histories[open_id].append({"role": "assistant", "content": reply})

        # 5) reply 最终回复
        try:
            r_reply = await _post_json_async(
                client,
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
                headers=headers,
                json_body={
                    "msg_type": "text",
                    "content": json.dumps({"text": reply}, ensure_ascii=False),
                },
            )
            logger.info("[Lark] reply 发送状态: %d", r_reply.status_code)
        except Exception:
            logger.exception("[Lark] reply 异常")
    except Exception:
        logger.exception("[Lark] 异步处理消息失败")


def _on_message(data):
    """SDK 传入 P2ImMessageReceiveV1 对象。

    Phase 25 / b7 修复: 不再在 SDK dispatch 线程里同步发 HTTP, 改为把整个
    处理流程提交到专用异步 loop, 立即返回让 SDK 继续 dispatch 下一条。
    """
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

        # 1) 提交到异步 worker (Semaphore 限 LLM 并发)
        _submit_async(
            _handle_message_async(
                open_id=open_id,
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                system_prompt=system_prompt,
                history_snapshot=history,
            )
        )
    except Exception:
        logger.exception("处理消息失败 (提交到异步 worker 之前)")


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

# === Phase 25 / b7: 启动异步 worker + 注册 atexit 钩子 ===
# 必须在注册 WS handler / 启 SDK 之前, 否则 _on_message 提交时 worker 还没就绪。
_start_async_worker()
# atexit 钩子: 退出时关闭 AsyncClient。任务里写"atexit.register(client.stop)",
# 这里用同义函数替它注册: 确保 _stop_async_worker 在进程退出时被调用。
atexit.register(_atexit_close_client)
logger.info("start_lark_sync: atexit hook 已注册 (进程退出时关 AsyncClient)")

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
# 给 atexit 用: 关 lark WS client
_lark_client_ref[0] = client

logger.info("启动 WS 客户端 (同步模式, 记忆窗口=%d)...", HISTORY_WINDOW)
try:
    client.start()
except KeyboardInterrupt:
    logger.info("已停止")
