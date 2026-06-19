# openclaw-py

> **OpenClaw 的 Python 全量重写** — 异步、模块化、可扩展的 AI Agent 运行时。

OpenClaw(原 ClawdBot / Moltbot) 是一个开源、MIT 协议、本地优先的 AI Agent 框架,使用 TypeScript/Node.js 编写 [1]。本项目用 **Python 3.10+** 重写,目标是对齐原版的全部能力面,提供:

- 🤖 异步 Agent Loop(ReAct 风格)
- 🔌 **多模型** :OpenAI 兼容 / Anthropic Claude / Google Gemini / Ollama 本地
- 🔀 **ProviderRouter**:fallback / round_robin / cost-aware / priority 四种策略
- 🪜 **Plan-Execute**:DAG 拓扑,同层并行,失败重试 + critical 短路
- 🎭 **Multi-Agent**:Planner / Executor / Critic / Reflector 四角色编排
- 🛠 **工具注册** :自动从签名生成 JSON Schema,支持同步+异步
- 🧰 **工具全量** :shell / fs / http / datetime / cron / docker 沙箱,带分类/权限/审批
- 🧠 **完整记忆** :SQLite 短期 + ChromaDB 长期向量 + SOUL.md / AGENTS.md 文档
- 🪪 **多 Scope 隔离** :session / user / channel / global
- 💬 消息渠道:CLI / 飞书长连接
- 🛡 **Auto-Reply** :黑/白名单 / 模板回复 / quiet hours / 限流 / 自定义判定
- 🧩 **Skills** :SKILL.md + skill.py 目录化,自动注入工具 + prompt
- 🔌 **插件体系** :entry_points + 本地目录扫描
- 🚌 **事件总线** :进程内 pub/sub + 可选 Redis Streams
- 📦 **统一配置** :YAML/JSON/TOML + 环境变量覆盖 + 热重载
- 📊 **结构化日志** :structlog + trace_id
- 🚦 统一异常体系
- ♻️ 与 TS 版 OpenClaw 的 `SOUL.md` / `AGENTS.md` 目录结构兼容

## 项目结构

```
openclaw_py/
├── openclaw/
│   ├── core/             # 基础设施:logging/config/bus/plugin/errors/auto_reply/rate_limit
│   ├── providers/        # 4 种 LLM 适配 + factory + router
│   ├── llm/              # 抽象接口 (BaseLLMProvider) + 数据类
│   ├── tools/            # 工具注册 + 内置工具
│   ├── agent/            # AgentLoop + Plan-Execute + Multi-Agent(已接入 ScopedMemory)
│   ├── memory/           # short_term / long_term / soul / workspace / scoped
│   ├── channels/         # CLI / 飞书 / Telegram / Discord / Slack / WhatsApp / Signal / iMessage
│   ├── config/           # 旧 .env Pydantic Settings(兼容)
│   └── cli.py            # Typer 入口
├── examples/
│   ├── hello_agent.py        # Phase 0 演示
│   ├── full_stack_demo.py    # Phase 1+2+3 全栈演示
│   ├── tools_demo.py         # Phase 4 工具全量演示
│   ├── phase5_smoke.py       # Plan-Execute / Multi-Agent / Router 端到端
│   ├── phase6_smoke.py       # Auto-Reply + Skills 端到端
│   └── phase7_smoke.py       # 多渠道入站 + ChannelManager + 真 LLM 端到端
├── tests/                 # 112 个测试
├── pyproject.toml
├── .env.example
└── README.md
```

## 快速开始

### 1. 安装

```bash
cd openclaw_py
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # 必需依赖
pip install -e ".[all]"          # 选装:playwright/docker/fastapi/redis
```

### 2. 配置(两种方式)

**方式 A:.env(向后兼容,Phase 0 老用户)**
```bash
cp .env.example .env
# 至少填 OPENAI_API_KEY
```

**方式 B:openclaw.yaml(推荐,Phase 1+ 全功能)**
```yaml
default_provider: main
providers:
  - name: openai_compat
    model: deepseek-chat
    api_key: sk-xxx
    base_url: https://api.deepseek.com/v1
  - name: ollama            # fallback
    model: llama3.1
    base_url: http://localhost:11434/v1
  - name: anthropic
    model: claude-3-5-sonnet-20241022
    api_key: sk-ant-xxx
agent:
  system_prompt: 你是 Claw
  max_tool_iterations: 8
  history_window: 20
  soul_paths: [./SOUL.md, ./AGENTS.md]
memory:
  dir: ./.openclaw_memory
  long_term_enabled: true
tools:
  fs_root: ./
  shell_default_cwd: ./
  shell_allowed: [ls, echo, cat]      # 留空 = 全部禁
  http_allowed_hosts: [example.com]   # 留空 = 全部禁
  include: [shell_exec, read_file, write_file, get_current_time, ...]
  exclude: [docker_*]
logging:
  level: INFO
  json: false              # 本地开发:控制台;生产:true
```

环境变量覆盖:`OPENCLAW_AGENT__SYSTEM_PROMPT=...` `OPENCLAW_LOGGING__LEVEL=DEBUG`

### 3. 跑

```bash
# CLI REPL
openclaw run                          # 用 .env
openclaw run --config openclaw.yaml   # 用配置文件

# 单次调用
openclaw once "用一句话介绍 Python 协程"
openclaw once "现在几点?然后算 7*8" --session demo

# 检查 SOUL 加载
openclaw soul

# 飞书(填 LARK_APP_ID/SECRET)
openclaw lark

# 全栈演示(不依赖网络)
python examples/full_stack_demo.py
```

## SOUL 文档格式(兼容 TS 版)

```markdown
---
scope: user:alice     # 可选: global | session:<id> | user:<id> | channel:<kind>:<id>
---

# 我是 Claw

我是一只本地龙虾,爱吃工具调用。

## 行为准则
- 简洁高效
- 不编造事实
```

加载路径(按顺序,先存在的优先):
1. `./SOUL.md`
2. `./AGENTS.md`
3. `./.openclaw/SOUL.md`
4. `~/.openclaw/SOUL.md`
5. `./knowledge/**/*.md`(每个文件作为一段 system)

## 工具全量(Phase 4)

`openclaw.tools.builtin.register_builtin_tools(registry, ...)` 一次性注册 6 大类工具:

| 分类 | 工具 | 权限 | 说明 |
|---|---|---|---|
| utility | `calculator` / `echo` | SAFE | 内置算子 |
| fs | `read_file` / `write_file` / `append_file` / `list_dir` / `search_files` / `file_stat` | READ/WRITE | 默认禁止越权,默认禁止覆盖 |
| shell | `shell_exec` | EXEC | subprocess + CWD/超时/白名单/拒 metachar |
| http | `http_get` / `http_post` / `http_request` | NETWORK | httpx + host 白名单 |
| datetime | `get_current_time` / `format_time` / `parse_time` / `timezone_convert` / `date_diff` | SAFE | 全部 IANA 时区 |
| cron | `cron_add` / `cron_list` / `cron_remove` | READ/WRITE | APScheduler,支持 cron/interval/one-shot |
| sandbox | `docker_run_python` / `docker_exec` / `docker_pull` / `docker_list_images` | ADMIN/READ | 可选 docker SDK,缺失时降级 |

```python
from openclaw.tools.builtin import register_builtin_tools
from openclaw.tools.registry import ToolRegistry, ToolPermission

reg = ToolRegistry()
register_builtin_tools(
    reg,
    fs_root="./workspace",
    shell_default_cwd="./workspace",
    shell_allowed=["ls", "echo", "cat"],          # 留空表全禁
    http_allowed_hosts=["example.com"],            # 留空表全禁
    include=["shell_exec", "read_file", "..."],    # 白名单
    exclude=["docker_*"],                          # 黑名单
)

# 危险工具走审批(EXE/ADMIN 自动开启)
async def ask_user(name, args): ...
reg.set_approver(ask_user)

out = asyncio.run(reg.call("shell_exec", {"command": "ls -l", "timeout": 5}))
```

完整烟测:`python examples/tools_demo.py`

### 真实模型烟测(可选)

用 `openclaw.agnes.yaml` 里配置的 `agnes-2.0-flash` 跑端到端 5 个场景:

```bash
python examples/p4_agnes_smoke.py
```

实测输出(`/tmp/p4_smoke2.log`):

| 场景 | iter | tool_calls | 实际调用的工具 |
|---|---|---|---|
| `calc` 137×89+256 | 2 | 1 | `calculator` |
| `shell` ls -la 工作目录 | 2 | 1 | `shell_exec`(返回真实目录内容) |
| `fs` 读 todo.md + echo | 3 | 2 | `read_file` + `echo` |
| `time` UTC 时间差 | 4 | 3 | `get_current_time` + `date_diff`(LLM 第一次时区错了,自动 retry) |
| `cron` 加 300s 周期 | 3 | 2 | `cron_add` + `cron_list` |

期间修了 3 个生产 bug:
- `OpenAICompatProvider` 跨 `asyncio.run` 边界 httpx client 残留 → 加 `_client_loop_id` 重建
- `cron_add(every_seconds)` LLM 传 `"300"` 字符串 → 加 `int()` 容错
- `date_diff` LLM 混传 naive/aware → 统一按 UTC fallback

## Agent 全量(Phase 5)

### Plan-Execute(`openclaw.agent.planner` + `executor`)

```python
from openclaw.agent import Plan, PlanStep, StepKind, PlanExecutor

plan = Plan(goal="汇总日报", steps=[
    PlanStep(id="t1", kind=StepKind.TOOL, target="shell_exec",
             arguments={"command": "ls -la"}),
    PlanStep(id="t2", kind=StepKind.TOOL, target="read_file",
             arguments={"path": "report.md"}, depends_on=["t1"]),
    PlanStep(id="s1", kind=StepKind.LLM, target="总结成一段话",
             depends_on=["t1", "t2"]),
])
ex = PlanExecutor(on_llm=..., on_tool=..., max_parallel=4)
result = asyncio.run(ex.run(plan))
```

特性:
- `topological_layers()` 给出同层可并行的分组
- `validate()` 检测未知依赖 / 环
- `max_retries` 单步重试
- `critical=False` 失败不短路后续
- `max_parallel` 限流

### Multi-Agent(`openclaw.agent.MultiAgentRoles`)

四角色:
- **Planner**: 把用户问题拆成 Plan JSON(失败时 fallback 到单 step)
- **Executor**: 跑 Plan,每步可调 LLM/工具
- **Critic**: 校验最终答案是否回答了用户问题、是否与工具事实一致
- **Reflector**: 失败 step 给改进建议,改 plan 后重跑(可配置循环次数)

```python
from openclaw.agent import MultiAgentRoles

ma = MultiAgentRoles(
    llm=router, tools=tools, memory=scoped,
    session_id="user-123",
    enable_critic=True, enable_reflector=True,
    max_reflection_loops=1,
)
res = await ma.run("现在几点?然后计算 7*8+15")
# res.plan / res.execution / res.final_answer / res.critic / res.reflections
```

真实模型烟测:`python examples/phase5_smoke.py`
- Q1: 拆 2 步(get_time + llm 算 60x),Critic 抓到"答案与工具输出事实不一致"
- Q2: 单步 cat 文件,Critic 给 ok=True,score=1.0

### Router 策略(`openclaw.providers.ProviderRouter`)

```yaml
agent:
  router_strategy: fallback_only   # fallback_only | round_robin | cost_aware | priority
  step_max_attempts: 2
  provider_costs:                   # cost_aware 必填
    agnes-2.0-flash: 0.1
    deepseek-chat: 0.5
  provider_priorities:              # priority 必填
    agnes-2.0-flash: 1
    deepseek-chat: 5
```

四种策略:
- **fallback_only**(默认):主失败 → 按 fallback 列表依次重试
- **round_robin**:每次调用把 primary 推到队尾,均衡负载
- **cost_aware**:按 `cost_per_1k` 从低到高选,失败后再切下一个
- **priority**:按 `priority` 数字升序选(数字小优先)

`RouterStats` 累计 `calls / failures / total_ms / by_provider`,便于监控和调优。

## Auto-Reply + Skills(Phase 6)

### Auto-Reply(`openclaw.core.AutoReplyManager`)

在 LLM 之前的消息路由器,做 5 件事:

1. **黑名单**:正则匹配 → 直接丢弃(危险词)
2. **静默时段**:夜间 23:00-07:00 → 静默
3. **模板回复**:关键词命中 → 直接给模板(不打 LLM,省 token)
4. **触发判定**:白名单关键词 / @bot / 私聊 / 自定义回调
5. **限流**:per-user / per-channel token bucket,可持久化到 sqlite

```python
from openclaw.core import AutoReplyConfig, AutoReplyManager, RateLimiter

arm = AutoReplyManager(AutoReplyConfig(
    triggers=["bot", "claw"],
    blacklist=[r"rm\s+-rf", r"格式化"],
    templates={"ping": "pong", "时间": "现在是 2026-06-19"},
    auto_in_dm=True, auto_when_mentioned=True,
    rate_per_user=RateLimiter(rate=0.5, burst=2),  # 每 2s 1 条,突发 2
    rate_per_channel=RateLimiter(rate=5.0, burst=10),
    quiet_hours=("23:00", "07:00"),
))

decision = await arm.decide(user_id="u1", channel="feishu", text="bot 在么")
if decision.reply:                       # 模板命中
    await channel.send(session, decision.reply)
elif not decision.passthrough:           # 被黑名单/限流/静默
    return
# 否则把 text 交给 AgentLoop,prompt_prefix 拼到 system_prompt
resp = await agent.handle(session, decision.prompt_prefix + text)
```

### Skills(`openclaw.core.SkillLoader`)

一个 Skill = 一个目录:

```
my_skill/
├── SKILL.md       # 必填: name/version/triggers/description(YAML front matter)
└── skill.py       # 可选: register(skill_api),可注册工具 + 注入 prompt
```

```python
from openclaw.core import load_skills
from openclaw.tools.registry import ToolRegistry

reg = ToolRegistry()
sreg = load_skills("./openclaw_skills", "./examples/skills", registry=reg)
# 工具已注入 reg,prompt_injections 可拼到 system_prompt
prompt = cfg.agent.system_prompt + "\n\n" + sreg.prompt_injections()
```

`examples/skills/` 提供了 3 个示例:`joke` / `weather` / `system_status`。

真实模型烟测:`python examples/phase6_smoke.py`
- ✅ 笑话 skill 调 `random_joke` 工具
- ✅ 黑名单拦截 `rm -rf`
- ✅ 天气 skill 调 `weather_query` 工具
- ✅ 模板回复 `ping` → `pong`(不打 LLM)
- ✅ 系统状态 skill 调 `system_status` 工具

## 多渠道(Phase 7)

### 渠道抽象(`openclaw.channels`)

所有渠道都实现相同的 `BaseChannel` 接口,把消息归一为 `IncomingMessage`,
经过 **AutoReply 决策 → AgentLoop 处理 → 主动 send** 的统一管道。`ChannelManager`
负责协调多渠道共享同一个 agent / auto_reply / on_reply 回调。

```python
from openclaw.channels import ChannelManager, EchoChannel, TelegramChannel
from openclaw.core import AutoReplyConfig, AutoReplyManager, RateLimiter

arm = AutoReplyManager(AutoReplyConfig(
    triggers=["bot"], blacklist=[r"rm\s+-rf"],
    templates={"ping": "pong"},
    rate_per_user=RateLimiter(rate=0.5, burst=2),
))

mgr = ChannelManager(agent_loop=agent, auto_reply=arm)
mgr.register(EchoChannel())                 # 测试用,无外部依赖
mgr.register(TelegramChannel.from_env(agent))  # 从 env 读 TELEGRAM_BOT_TOKEN
# await mgr.start_all()                      # 阻塞直到 stop_all()
```

支持的渠道(均为同一种 `BaseChannel` 范式):

| 渠道 | 协议 | 入口 | 备注 |
|---|---|---|---|
| `EchoChannel` | 测试桩 | `dispatch()` | 无外部依赖,灌入即收 |
| `CLIChannel` | REPL | stdin | 终端交互 |
| `LarkChannel` | 飞书长连接 | WS | 旧版,保持兼容 |
| `TelegramChannel` | Bot API | long polling | `send` 自动 4000 字切分 |
| `DiscordChannel` | Interactions API | webhook + 可选 gateway | slash 命令 + Ed25519 验签(可选) |
| `SlackChannel` | Events API | webhook | HMAC-SHA256 验签(`X-Slack-Signature`),自动去 `<@BOTID>` |
| `WhatsAppChannel` | Cloud API | webhook | 4096 字切分,`verify_webhook` 握手 |
| `SignalChannel` | signal-cli REST | long polling | 容错同时支持 `envelope.source` 与 `env.source` |
| `IMessageChannel` | BlueBubbles | webhook | macOS only,非 Darwin 直接报错 |

环境变量:

| 渠道 | 必填 env |
|---|---|
| Telegram | `TELEGRAM_BOT_TOKEN` |
| Discord | `DISCORD_BOT_TOKEN`, `DISCORD_PUBLIC_KEY` |
| Slack | `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` |
| WhatsApp | `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_ID`, `WHATSAPP_VERIFY_TOKEN` |
| Signal | `SIGNAL_CLI_URL` (默认 `http://localhost:8080`), `SIGNAL_ACCOUNT` |

### ChannelManager 行为
- `register()` 自动注入 `agent_loop` / `auto_reply` / `on_reply`,避免每个 channel 重复构造
- `start_all()` 用 `asyncio.gather` 拉起所有 channel,任一抛错会传播(便于 fail-fast)
- `BaseChannel.dispatch(msg)` 走 4 步:AutoReply 决策 → 模板命中直接发 → 丢弃(黑名单/未触发)→ AgentLoop 处理

### 真实模型烟测

`python examples/phase7_smoke.py`(输出见 `/tmp/p7_smoke.log`):

| 段 | 验证点 | 结果 |
|---|---|---|
| [1] | ChannelManager 依赖注入(`agent_loop` / `auto_reply`) | ✅ |
| [2] | 6 个 channel 的入站解析(telegram/discord/slack/whatsapp/signal/imessage) | ✅ |
| [3] | EchoChannel 统一管道 — template / blacklist / 白名单 / DM 默认放行 / 限流 | ✅ |
| [4] | 真 LLM 端到端 — `bot 用 shell_exec 跑 date` → 答出"当前时间" | ✅ |

## 写自己的工具

```python
from openclaw.tools import ToolRegistry
from openclaw.tools.builtin import register_builtin_tools

reg = ToolRegistry()
register_builtin_tools(reg)

@reg.tool
def search_docs(query: str, top_k: int = 5) -> str:
    """在公司知识库里检索。query: 关键词; top_k: 返回条数。"""
    return "..."
```

## 写自己的插件

```python
# my_plugin.py
def register(runtime):
    @runtime.register_tool
    class MyTool:
        name = "my_tool"
        description = "..."
        parameters = {"type": "object", "properties": {...}}
        async def __call__(self, **kwargs): ...

# pyproject.toml
[project.entry-points."openclaw.plugins"]
my_plugin = "my_plugin:register"
```

## 当前完成度(2026-06)

| 阶段 | 状态 | 产出 |
|---|---|---|
| Phase 0:基线(MVP) | ✅ | 17 测试 |
| Phase 1:基础设施(L0) | ✅ | core/logging + config(热重载) + bus + plugin(33 测试) |
| Phase 2:多 LLM(L1) | ✅ | OpenAI 兼容 + Anthropic + Gemini + Ollama + Router(fallback/round-robin) |
| Phase 3:完整记忆(L2) | ✅ | short_term / long_term(ChromaDB)/ soul / workspace / scoped |
| Phase 4:工具全量(L3) | ✅ | shell / fs / http / datetime / cron / docker 沙箱,带分类/权限/审批 |
| Phase 5:Agent 全量(L4) | ✅ | Plan-Execute DAG + Multi-Agent(Planner/Executor/Critic/Reflector) + Router 四策略 |
| Phase 6:Auto-Reply + Skills(L5) | ✅ | 模板/限流/黑名单 + SKILL.md 目录加载(工具 + prompt 注入) |
| Phase 7:多渠道(L6) | ✅ | CLI / 飞书 / Telegram / Discord / Slack / WhatsApp / Signal / iMessage(112 测试) |
| Phase 8:Gateway(L7) | 🔲 | FastAPI REST + Web UI |

## 开发

```bash
pip install -e ".[dev,all]"
ruff check .
pytest -q
```

## 许可证

MIT

## 参考

[1] OpenClaw GitHub: <https://github.com/openclaw/openclaw>
