# Changelog

openclaw-py-m3 的所有值得注意的变更记录。
版本遵循 [Semantic Versioning](https://semver.org/),格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)。

---

## [Unreleased]

### Security (Phase 27)

- **C1** 修复 `create_app` 的 `type(...)` 异质联合默认值 → `_DefaultRateLimiterSentinel` 类
- **C2** 修复 `root_index` 路由从 `if static_dir.exists()` 内提到顶层(无论 `static/` 是否存在,`/` 都可访问)
- **C3** 修复 `cron add` 走 `shell=True` 的 RCE 通道:新增 `_validate_command` 拒绝 shell metachar + 解释器黑名单,subprocess 改 `shell=False`
- **C4** 修复 `_merge_env` 用 `model_dump()` 把 `SecretStr` 序列化为占位符,导致生产部署 yaml + env 注入路径下鉴权静默失败。改用 `model_dump(mode="python")` + `_deep_merge_secretsafe` 保护 secret 字段
- **C5** 修复 `journal.py` 路径越界用 `str.startswith` 字符串拼接易绕过。改用 `Path.is_relative_to`(Python 3.9+),加拒绝绝对路径 / NUL 三层加固
- **H1** 修复 `openai_compat` 跨 loop 重建时 `aclose` 失败端口泄露,改用 `asyncio.shield` 包裹
- **H3** 修复 prod 模式 + 配 token + 缺 user_id + 缺 token_to_user 仍启动 → `RuntimeError` 阻断(防止 per-user 隔离蒸发)
- **M3/H6** 修复 `agent_loop.trim_history` O(n²) 字符计数 → 维护 `cur_chars` 单调递减的局部变量
- **M3** 修复 `AgentLoop.handle` 缺外层超时,加 `asyncio.wait_for` 默认 300s(env `OPENCLAW_AGENT_HANDLE_TIMEOUT` 可覆盖)
- **M5** 修复 `memory` 路由 7 处 `try/except: raise HTTPException(500, f"{str(e)}")` 泄漏底层异常。新增 `_safe_http_500` 统一脱敏
- **M5** 修复 `gateway/util.py` 5 处 `except Exception: pass` 无声吞错 → `logger.debug` 记录
- **M9** 修复 prod + `OPENCLAW_GATEWAY_DEV=1` 矛盾配置不阻断 → `RuntimeError` 阻断
- **M11** 修复 `channels/base.py:start_all` 用 `return_exceptions=False` 任一 channel 失败就 crash manager → 改 `return_exceptions=True`
- **M13** 修复鉴权失败仅 INFO 日志 + 无指标 → 升级 WARNING + 新增 `gateway_auth_rejected_total` Counter
- **M6** 修复 `discord.py` `start()` 重复 pynacl 检查(`__init__` 已 fail-fast,start 必到不了)
- **M10** 修复 `ChannelRuntimeConfig.webhook_host` 默认 `0.0.0.0`(对外暴露)→ `127.0.0.1`
- **M11** 修复限流 key 在反代后失效(全 proxy_addr 共享一桶)→ 优先 `X-Forwarded-For`(需 `OPENCLAW_GATEWAY_TRUSTED_PROXY=1` 显式开启)
- **M12** 修复 Playwright `--no-sandbox` 无法关闭 → 加 `OPENCLAW_PLAYWRIGHT_NO_SANDBOX=0` 显式关
- **M15** 把 `gateway/app.py` 的中间件装配抽到 `_install_middlewares` 私有函数,主 `create_app` 流程剩 30 行
- **M19** 修复 `docker-compose.yml` 默认 `OPENCLAW_GATEWAY_TOKEN` 为空串 → 改 `${VAR:?error}` 显式报错

### Reliability

- **M2** 修复 `AgentLoop.handle` 缺顶层 try/except + 异常脱敏(`AgentResponse.error_type` 新字段,str(e) 不再泄漏)
- **M4** 修复 `openai_compat` 429/5xx 不重试 → 加指数退避 `(0.5, 1.0, 2.0)` 最多 3 次;4xx 立即抛
- **M6** 修复 `_get_message_store` lazy init 非线程安全 → 加 `threading.Lock`
- **M7** 修复 `journal` 路由同步 I/O 阻塞 event loop → `read_text` / `list_entries` / `weekly_report` 全部走 `asyncio.to_thread`
- **M12** 修复 `_get_agent` 并发竞争 → 同步路径加 `threading.RLock`;新增 `async aget_agent` + `asyncio.Lock`
- **M14** 抽 `chat` 与 `chat_stream` 公共业务到 `_process_chat_turn` helper
- **M18** `ConfigError` 加 `path=...` 关键字参数,`__str__` 输出 `[config: <path>] <msg>`(无 path 时零回归)
- **M22** 修复 `journal.reflect` 调用 `generate_soul_proposal` 但丢弃返回值 → 收下 `proposal_path` + `logger.debug` 记录(不破坏 reflect 返回 str 反思文本的 BC)

### Documentation (Phase 27 / H6-H8, M23-M24)

- **H6** README 头部新增 ASCII 架构图 + 5 条关键不变量
- **H7** 新增本 `CHANGELOG.md`
- **H8** `CONTRIBUTING.md` 新增"提交 PR 前清单"(跑测试 / ruff / 是否带测试)
- **M23** 新增 `docs/plugin-development.md` —— 写自己插件的完整示例 + `register_xxx_tools` 模板
- **M24** 新增 `docs/deployment.md` —— Docker / systemd / 反代 / TLS / 监控 / 备份 6 个场景的部署清单

### Notes for Upgraders

- **BC**: `AgentResponse` 仅加 `error_type: str | None = None` 字段(默认 None,所有现有调用零变化)
- **BC**: `journal.reflect()` 仍返回 str(反思文本),不返回 list(避免破坏现有 caller)
- **BC**: `OpenClawConfig._merge_env` 改名为 `merge_with_env`(Pydantic v2 不暴露下划线前缀方法)
- **Config**: `ChannelRuntimeConfig.webhook_host` 默认从 `0.0.0.0` → `127.0.0.1`,如有外网监听需求请显式设 `0.0.0.0`
- **Config**: `OPENCLAW_GATEWAY_TRUSTED_PROXY=1` 现在可选,默认仍走 `client.host`(防止误信任伪造 X-Forwarded-For)
- **Docker**: `docker-compose.yml` 启动期 `OPENCLAW_GATEWAY_TOKEN` 未设会**报错**(`?error:missing-OPENCLAW_GATEWAY_TOKEN`)

---

## 旧版本

历史版本变更散落在 git commit history + README "当前完成度" 段落。
本 CHANGELOG 从 Phase 27(2026-06-22)开始正式维护。
