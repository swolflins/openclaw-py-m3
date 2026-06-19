"""飞书配置探针 + 错误码表 + 后台操作建议。

设计原则:
- 5 个端点全部用 tenant_access_token 调,无需 user_access_token
- 端点原始响应 + 解析后的状态字段,都返回给调用方(报告用)
- 错误码 → 中文说明 + 后台操作路径(URL + 按钮位置)
- 不依赖 lark-oapi(只走 httpx),不发起 WS 长连接
- 单测可独立:不依赖网络
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

LARK_BASE = "https://open.feishu.cn/open-apis"
APP_BASE = "https://open.feishu.cn/app"


# ─────────────── 错误码查表 ───────────────

# 覆盖 飞书 im / bot / auth / application 域最常见的 20+ 错误码。
# 每条: 域 + code → { 标题, 原因, 后台路径, 操作步骤, 文档 URL }
ERROR_TABLE: dict[tuple[str, int], dict[str, str]] = {
    # ── 域: auth ──
    ("auth", 10003): {
        "title": "App ID / Secret 错误",
        "reason": "app_id 不存在 或 app_secret 与之不匹配",
        "fix": "去飞书开发者后台核对 app_id,确认 secret 没被重置过(必要时重置)",
        "url": "https://open.feishu.cn/app",
    },
    ("auth", 10014): {
        "title": "App Secret 错误",
        "reason": "app_secret 与 app_id 不匹配(可能重置过 / 输错)",
        "fix": "在后台 凭证与基础信息 → App Secret → 重置,更新 .env",
        "url": "https://open.feishu.cn/app",
    },
    ("auth", 20001): {
        "title": "App ID / Secret 错误",
        "reason": "app_id 不存在 或 app_secret 与之不匹配",
        "fix": "去飞书开发者后台核对 app_id,确认 secret 没被重置过(必要时重置)",
        "url": "https://open.feishu.cn/app",
    },
    ("auth", 20002): {
        "title": "App Secret 已过期 / 被重置",
        "reason": "app_secret 已经被重置,旧的失效",
        "fix": "在后台 凭证与基础信息 → App Secret → 重置,更新 .env",
        "url": "https://open.feishu.cn/app",
    },
    ("auth", 20003): {
        "title": "App 已停用 / 已删除",
        "reason": "应用被管理员禁用或删除",
        "fix": "在飞书后台启用应用;如已删除需重新创建",
        "url": "https://open.feishu.cn/app",
    },
    ("auth", 99991663): {
        "title": "App 在审核中 / 不可用",
        "reason": "App 状态不是 active(可能审核中 / 已下架)",
        "fix": "在飞书后台查看 App 状态;如审核中,等审核通过",
        "url": "https://open.feishu.cn/app",
    },

    # ── 域: bot / im.send ──
    ("im", 230001): {
        "title": "请求参数错误",
        "reason": "body 字段缺失 / 类型不对 / msg_type 与 content 不匹配",
        "fix": "对照 im/v1/messages 文档:msg_type=text 时 content 是 '{\"text\": \"...\"}' 字符串",
        "url": "https://open.feishu.cn/document/server-docs/im-v1/message/create",
    },
    ("im", 230002): {
        "title": "Bot 不可用",
        "reason": "机器人能力未开启,或被禁用",
        "fix": "应用详情 → 添加应用能力 → 机器人 → 启用",
        "url": "https://open.feishu.cn/app",
    },
    ("im", 230006): {
        "title": "没有 im:message 发送权限",
        "reason": "App 没申请 im:message 这一组 scope",
        "fix": "权限管理 → API 权限 → 搜 'im:message' → 申请 '发送消息'",
        "url": "https://open.feishu.cn/app",
    },
    ("im", 230013): {
        "title": "Bot 对该用户/群 无可用性",
        "reason": "App 未发布 / 未上线,或可见范围没包含目标用户",
        "fix": "版本管理与发布 → 创建版本 → 申请上线(至少'自建应用仅自用');"
               "权限管理 → 可见范围 → 选'全部员工'或指定部门",
        "url": "https://open.feishu.cn/app",
    },
    ("im", 230020): {
        "title": "Bot 不在 chat 中",
        "reason": "Bot 还没被任何用户/群拉进去;p2p 需要用户先发消息或主动 add",
        "fix": "在飞书搜索 bot 名字,发起私聊,或把它加到一个群里",
        "url": "https://open.feishu.cn/app",
    },
    ("im", 230021): {
        "title": "Bot 被踢出 chat",
        "reason": "Bot 之前在这个 chat,现在不在了",
        "fix": "重新拉 bot 进 chat 后再发",
        "url": "https://open.feishu.cn/app",
    },
    ("im", 230022): {
        "title": "Bot 没消息发送权限(群内)",
        "reason": "Bot 在群里,但没拿到该群的发言权",
        "fix": "群主/管理员在群设置里给 bot 开'发言'权限",
        "url": "https://open.feishu.cn/app",
    },
    ("im", 230025): {
        "title": "消息内容超长 / 含敏感词",
        "reason": "单条消息 >30KB 或含违禁词",
        "fix": "拆分消息,或检查 text 是否含 url/特殊字符需要审核",
        "url": "https://open.feishu.cn/document/server-docs/im-v1/message/create",
    },
    ("im", 230035): {
        "title": "Bot 不在租户内",
        "reason": "App 创建在另一个飞书租户",
        "fix": "用 bot 所在租户的企业账号登录飞书,再发消息",
        "url": "https://open.feishu.cn/app",
    },
    ("im", 230049): {
        "title": "跨租户单聊",
        "reason": "Bot 试图给非本租户用户发私聊",
        "fix": "确认 receive_id 来自同一租户(用 open_id 拉 user 看 tenant_key)",
        "url": "https://open.feishu.cn/document/server-docs/im-v1/message/create",
    },
    ("im", 230054): {
        "title": "消息对当前操作者不可见",
        "reason": "Bot 试图 reply / recall 看不见的消息",
        "fix": "只对 bot 自己发送的消息 reply,或直接 send 新消息",
        "url": "https://open.feishu.cn/document/server-docs/im-v1/message/create",
    },
    ("im", 230099): {
        "title": "IM 通用错误",
        "reason": "看 msg 字段",
        "fix": "把 code + msg 贴给飞书技术支持",
        "url": "https://open.feishu.cn/document/server-docs/im-v1/message/create",
    },
    ("im", 231022): {
        "title": "Bot 对该消息无法 reaction(可见性限制)",
        "reason": "App 可见范围不包含消息作者",
        "fix": "后台 → 可见范围 → 加上作者所在部门 / 全部员工",
        "url": "https://open.feishu.cn/app",
    },
    # ── 域: application ──
    ("application", 210504): {
        "title": "App 未发布版本",
        "reason": "商店应用必须先发布,自建应用一般无此限制",
        "fix": "版本管理 → 创建版本 → 发布",
        "url": "https://open.feishu.cn/app",
    },
    ("application", 230027): {
        "title": "App 没有这个 API 权限",
        "reason": "本端点需要特定 scope,当前 App 没申请",
        "fix": "权限管理 → API 权限 → 搜对应权限名 → 申请",
        "url": "https://open.feishu.cn/app",
    },
    # ── 域: 通用 ──
    ("common", 99991663): {
        "title": "消息内容审核拦截",
        "reason": "msg 含敏感词 / url 黑名单 / 超长",
        "fix": "检查 msg 文本;如果走卡片,用 text 而不是 markdown",
        "url": "https://open.feishu.cn/document/server-docs/im-v1/message/create",
    },
    ("common", 99992402): {
        "title": "请求参数错误",
        "reason": "缺少必填字段 / 字段类型不对",
        "fix": "对照文档核对 body 字段名 / 类型",
        "url": "https://open.feishu.cn/document/server-docs/im-v1/message/create",
    },
    ("common", 99991400): {
        "title": "请求超时",
        "reason": "服务端处理超过 3s",
        "fix": "重试;若是长任务,改异步",
        "url": "https://open.feishu.cn/document/server-docs/im-v1/message/create",
    },
    ("common", 99991672): {
        "title": "频率超限 / QPS 限流",
        "reason": "单 App / 单租户调用频率超阈值",
        "fix": "降频;需要更高 QPS 在后台申请配额",
        "url": "https://open.feishu.cn/app",
    },
    # ── 域: event 事件订阅 ──
    ("event", 10001): {
        "title": "事件订阅 URL 不可达",
        "reason": "回调 URL 验证失败(challenge 没正确返回)",
        "fix": "服务端在 3s 内返回 challenge;HTTPS + 有效证书 + 正确 path",
        "url": "https://open.feishu.cn/document/server-docs/event-subscription-guide/overview",
    },
    ("event", 10002): {
        "title": "事件订阅 已停用",
        "reason": "后台关掉了事件订阅",
        "fix": "事件订阅 → 开启;并把订阅方式切到'长连接接收'",
        "url": "https://open.feishu.cn/app",
    },
}


def lookup_error(domain: str, code: int) -> Optional[dict[str, str]]:
    """查表;code 找不到精确匹配时,fallback 找 (common, code) 或 (domain, 0)。"""
    if (domain, code) in ERROR_TABLE:
        return ERROR_TABLE[(domain, code)]
    if ("common", code) in ERROR_TABLE:
        return ERROR_TABLE[("common", code)]
    return None


# ─────────────── 探针 ───────────────

@dataclass
class ProbeResult:
    """单端点探测结果。"""
    name: str
    url: str
    status: str  # "ok" | "degraded" | "error"
    http: int = 0
    code: int = -1
    msg: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "status": self.status,
            "http": self.http,
            "code": self.code,
            "msg": self.msg,
            "data": self.data,
            "hint": self.hint,
        }


async def _get(c: httpx.AsyncClient, url: str, headers: dict[str, str],
               params: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    r = await c.get(url, headers=headers, params=params, timeout=15)
    try:
        body = r.json()
    except Exception:
        body = {"_raw": r.text[:200]}
    return r.status_code, body


async def probe_all(app_id: str, app_secret: str) -> dict[str, Any]:
    """对 app_id / app_secret 做 5 端点完整探针,返回报告 dict。"""
    if not app_id or not app_secret:
        return {"error": "LARK_APP_ID / LARK_APP_SECRET 至少一个为空"}

    report: dict[str, Any] = {
        "app_id": app_id,
        "probes": [],
    }

    async with httpx.AsyncClient(timeout=15) as c:
        # ── 1) tenant_access_token ──
        r = await c.post(
            f"{LARK_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        )
        body = r.json()
        if body.get("code") != 0 or not body.get("tenant_access_token"):
            err = lookup_error("auth", body.get("code", -1)) or {
                "title": "未知 auth 错误", "fix": "对照 msg 排查", "url": "https://open.feishu.cn/app"
            }
            report["probes"].append(ProbeResult(
                name="auth/tenant_access_token",
                url=f"{LARK_BASE}/auth/v3/tenant_access_token/internal",
                status="error",
                http=r.status_code,
                code=body.get("code", -1),
                msg=body.get("msg", ""),
                hint=f"{err['title']}: {err['fix']}",
            ).to_dict())
            report["abort_reason"] = err
            return report

        token = body["tenant_access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        report["token_expire_s"] = body.get("expire", 0)

        # ── 2) bot/v3/info(bot 身份)──
        http, body = await _get(c, f"{LARK_BASE}/bot/v3/info", auth)
        bot = body.get("bot", {}) if body.get("code") == 0 else {}
        report["bot"] = {
            "open_id": bot.get("open_id", ""),
            "app_name": bot.get("app_name", bot.get("name", "")),
            "status": "active" if bot else "unknown",
        }
        hint = ""
        if body.get("code") != 0:
            err = lookup_error("im", body.get("code", -1)) or {
                "title": "bot 不可用", "fix": "应用详情 → 添加应用能力 → 启用机器人"
            }
            hint = f"{err['title']}: {err['fix']}"
        report["probes"].append(ProbeResult(
            name="bot/v3/info",
            url=f"{LARK_BASE}/bot/v3/info",
            status="ok" if body.get("code") == 0 else "error",
            http=http, code=body.get("code", -1), msg=body.get("msg", ""),
            data={"open_id": bot.get("open_id", ""), "app_name": bot.get("app_name", "")},
            hint=hint,
        ).to_dict())

        # ── 3) application/v6/applications/:app_id(应用基础信息)──
        # 这端点需要应用已发布(自建未发布拿不到);失败就降级
        http, body = await _get(
            c,
            f"{LARK_BASE}/application/v6/applications/{app_id}",
            auth,
            params={"lang": "zh-CN", "user_id_type": "open_id"},
        )
        app_info = body.get("data", {}).get("app", {}) if body.get("code") == 0 else {}
        report["app_info"] = {
            "app_id": app_info.get("app_id", ""),
            "name": app_info.get("name", report["bot"].get("app_name", "")),
            "description": app_info.get("description", ""),
            "status": app_info.get("status", "unknown"),
            "primary_locale": app_info.get("primary_locale", ""),
        }
        # 这端点对未发布 app 一律 99992402,这不是 error,是"未发布"
        hint = ""
        if body.get("code") not in (0, 99992402):
            err = lookup_error("application", body.get("code", -1)) or {
                "title": "App 状态未知", "fix": "后台查看"
            }
            hint = f"{err['title']}: {err['fix']}"
        elif body.get("code") == 99992402:
            # 未发布:正常
            hint = "App 尚未发布(自建/未发布 app 此端点会 99992402,正常)。状态见上面 bot 段。"
        report["probes"].append(ProbeResult(
            name="application/v6/applications/:app_id",
            url=f"{LARK_BASE}/application/v6/applications/{app_id}",
            status="ok" if body.get("code") == 0 else "degraded",
            http=http, code=body.get("code", -1), msg=body.get("msg", ""),
            data=app_info,
            hint=hint,
        ).to_dict())

        # ── 4) /contact/v1/scope/get(通讯录授权范围)──
        # 端点用 tenant_access_token;不能查事件订阅(那个需 user_access_token)
        http, body = await _get(
            c,
            f"{LARK_BASE}/contact/v1/scope/get",
            auth,
        )
        scope = body.get("data", {}) if body.get("code") == 0 else {}
        report["contact_scope"] = {
            "authed_departments": scope.get("authed_open_departments", []),
            "authed_users": scope.get("authed_open_user_ids", []),
        }
        hint = ""
        if body.get("code") != 0:
            code = body.get("code", -1)
            if code == 99991672:
                # 区分:99991672 也可能是 "No permission" 而不是限流
                msg = body.get("msg", "")
                if "permission" in msg.lower() or "无权限" in msg:
                    hint = ("无 contact:contact:readonly 权限 — 自建 app 默认不开,"
                            "无法 API 查可见范围。请去后台 权限管理 → 可见范围 手动确认。")
                else:
                    err = lookup_error("application", code) or {
                        "title": "频率超限", "fix": "降频"
                    }
                    hint = f"{err['title']}: {err['fix']}"
            else:
                err = lookup_error("application", code) or {
                    "title": "无法查可见范围", "fix": "后台手动确认 权限管理 → 可见范围"
                }
                hint = f"{err['title']}: {err['fix']}"
        elif not scope.get("authed_open_departments") and not scope.get("authed_open_user_ids"):
            hint = ("可见范围为空 — App 不可见,所有发消息都会 230013。"
                    "后台 → 权限管理 → 可见范围 → 选'全部员工'或指定部门 / 用户")
        report["probes"].append(ProbeResult(
            name="contact/v1/scope/get",
            url=f"{LARK_BASE}/contact/v1/scope/get",
            status="ok" if body.get("code") == 0 else "degraded",
            http=http, code=body.get("code", -1), msg=body.get("msg", ""),
            data=report["contact_scope"],
            hint=hint,
        ).to_dict())

        # ── 4b) 事件订阅状态(无法用 API 直接查,改为提示)──
        report["event_subscriptions"] = {
            "count": None,
            "events": [],
            "api_unreachable": True,
            "manual_check": f"打开 {APP_BASE} → 选择本 app → 左侧'事件订阅' → 确认订阅方式 = '长连接接收' + 已添加 im.message.receive_v1",
        }
        report["probes"].append(ProbeResult(
            name="event/v1/subscriptions",
            url="(后端未暴露此 API 给 tenant_access_token,需手动在后台看)",
            status="degraded",
            http=0, code=-1, msg="",
            data={},
            hint=f"去 {APP_BASE} → 本 app → 事件订阅 → 确认 长连接接收 + im.message.receive_v1",
        ).to_dict())

        # ── 5) im/v1/chats(实际能发到哪)──
        http, body = await _get(c, f"{LARK_BASE}/im/v1/chats", auth, params={"page_size": 5})
        chats = body.get("data", {}).get("items", []) if body.get("code") == 0 else []
        report["bot_chats"] = {
            "count": len(chats),
            "items": [{"chat_id": c.get("chat_id", "")[:14] + "...",
                       "name": c.get("name", ""),
                       "type": c.get("chat_mode", c.get("chat_type", ""))} for c in chats],
        }
        hint = ""
        if body.get("code") != 0:
            err = lookup_error("im", body.get("code", -1)) or {
                "title": "无法列 chat", "fix": "后台确认 im:chat 权限已开"
            }
            hint = f"{err['title']}: {err['fix']}"
        elif not chats:
            hint = ("bot 当前在 0 个 chat — 没人拉它进群/私聊过。"
                    "在飞书搜 bot 名字,主动发起对话,或加到一个群里")
        report["probes"].append(ProbeResult(
            name="im/v1/chats",
            url=f"{LARK_BASE}/im/v1/chats",
            status="ok" if body.get("code") == 0 else "degraded",
            http=http, code=body.get("code", -1), msg=body.get("msg", ""),
            data=report["bot_chats"],
            hint=hint,
        ).to_dict())

    return report


# ─────────────── 报告渲染 ───────────────

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _use_color(force: bool | None = None) -> bool:
    if force is not None:
        return force
    return sys.stdout.isatty()


def _color(s: str, color: str, force_color: bool | None = None) -> str:
    if not _use_color(force_color):
        return s
    return f"{color}{s}{RESET}"


def render_report(report: dict[str, Any], force_color: bool | None = None) -> str:
    """把 probe_all 的返回渲染成人类可读的报告(带颜色)。"""
    use_c = _use_color(force_color)
    c = {
        "GREEN": GREEN if use_c else "",
        "YELLOW": YELLOW if use_c else "",
        "RED": RED if use_c else "",
        "BOLD": BOLD if use_c else "",
        "RESET": RESET if use_c else "",
    }

    out: list[str] = []
    out.append(f"\n{c['BOLD']}══════════════════════════════════════════{c['RESET']}")
    out.append(f"{c['BOLD']}  飞书后台配置诊断报告{c['RESET']}")
    out.append(f"{c['BOLD']}══════════════════════════════════════════{c['RESET']}\n")
    out.append(f"  app_id = {report.get('app_id', '?')}")
    if "bot" in report:
        bot = report["bot"]
        out.append(f"  bot    = {bot.get('app_name', '?')} ({bot.get('open_id', '?')[:14]}...)")
    if "app_info" in report:
        ai = report["app_info"]
        out.append(f"  app    = {ai.get('name', '?')} (status={ai.get('status', '?')})")
    if "event_subscriptions" in report:
        es = report["event_subscriptions"]
        out.append(f"  events = {es.get('count', 0)} 个已订阅" + (f" ({', '.join(es.get('events', [])[:5])}{' ...' if es.get('count', 0) > 5 else ''})" if es.get('count', 0) else ""))
    if "bot_chats" in report:
        bc = report["bot_chats"]
        out.append(f"  chats  = {bc.get('count', 0)} 个")
    out.append("")

    if "abort_reason" in report:
        ar = report["abort_reason"]
        out.append(f"  {c['RED']}{c['BOLD']}⛔ 凭据不可用,后续探测中止{c['RESET']}")
        out.append(f"     {ar.get('title', '?')}")
        out.append(f"     {ar.get('fix', '?')}")
        out.append(f"     文档: {ar.get('url', '?')}")
        return "\n".join(out)

    # 详细列每个 probe
    out.append(f"  {c['BOLD']}各端点详情:{c['RESET']}\n")
    for p in report.get("probes", []):
        status_color = {
            "ok": c["GREEN"], "degraded": c["YELLOW"], "error": c["RED"]
        }.get(p["status"], "")
        mark = {"ok": "✅", "degraded": "⚠️ ", "error": "❌"}.get(p["status"], "?")
        out.append(f"  {mark} {c['BOLD']}{p['name']}{c['RESET']}  →  {status_color}{p['status']}{c['RESET']}")
        out.append(f"     http={p['http']}  code={p['code']}  msg={p['msg'][:80]}")
        if p.get("hint"):
            out.append(f"     {c['YELLOW']}提示:{c['RESET']} {p['hint']}")
        out.append("")

    # 综合建议
    out.append(f"  {c['BOLD']}════════ 后台操作清单 ════════{c['RESET']}\n")
    todo = []
    if "bot" in report and not report["bot"].get("open_id"):
        todo.append("1. 应用详情 → 添加应用能力 → 机器人 → 启用")
    # 事件订阅:总有(手动查)
    if "event_subscriptions" in report:
        if not report["event_subscriptions"].get("events"):
            todo.append("2. 事件订阅 → 订阅方式选'长连接接收' → 添加事件 im.message.receive_v1")
    if "bot_chats" in report and report["bot_chats"]["count"] == 0:
        todo.append("3. 在飞书搜 bot 名字 → 发起私聊(把它拉进任何一个 chat)")
    todo.append("4. 权限管理 → API 权限 → 搜 'im:message' → 申请'发送消息'")
    todo.append("5. 版本管理与发布 → 创建版本 → 申请上线(至少'自建应用仅自用')")
    todo.append("6. 权限管理 → 可见范围 → 选'全部员工'或指定部门(否则 230013)")
    for t in todo:
        out.append(f"   {t}")
    out.append(f"\n  后台 URL: {APP_BASE}")
    out.append("\n  代码 → 后台定位:应用详情页左下角的'事件订阅'/'权限管理'/'版本管理与发布'/'应用能力'\n")
    return "\n".join(out)
