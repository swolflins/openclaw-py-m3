# Plugin Development Guide (Phase 27 / M23)

本指南讲如何给 openclaw-py-m3 写**自定义插件**。插件有 3 种形态,本仓库都支持。

## 1. 自定义工具(Tool)

工具是 Agent 在 ReAct 循环里能调用的函数。最小例子:

```python
# my_pkg/tools/greet.py
from openclaw.tools.registry import ToolDef, ToolParam

def greet(name: str) -> str:
    """Greet a person by name."""
    return f"Hello, {name}!"

TOOLS = [
    ToolDef(
        name="greet",
        description="Say hello to a person",
        func=greet,
        params=[
            ToolParam(name="name", type="string", description="Person's name", required=True),
        ],
    ),
]


def register_greet_tools(registry):
    """按 openclaw 约定的注册函数 — `tools.extras` 模块路径指向这个。"""
    for t in TOOLS:
        registry.register(t)
```

接入:在 `~/.openclaw/openclaw.yaml` 里加:

```yaml
tools:
  extras:
    - my_pkg.tools.greet
```

完整规范见 `openclaw/tools/registry.py` 的 `ToolDef` dataclass。

## 2. 自定义 Skill(SKILL.md)

Skill 是一段 markdown 描述 + 一个 Python 实现,Agent 在 prompt 里看到描述,按需调实现。

参考 `examples/skills/weather/` 完整示例。

## 3. 自定义 Provider(LLM 后端)

实现 `openclaw/llm/base.py:BaseLLMProvider` 接口(主要是 `async acomplete(...)` / `aclose()`):

```python
# my_pkg/providers/my_llm.py
from openclaw.llm.base import BaseLLMProvider, LLMResponse

class MyLLM(BaseLLMProvider):
    async def acomplete(self, messages, **kwargs) -> LLMResponse:
        # 调你的 LLM SDK,返回 LLMResponse
        ...
    async def aclose(self) -> None:
        # 清理 client
        ...
```

接入:在 `~/.openclaw/openclaw.yaml` 里加:

```yaml
providers:
  - name: my-llm
    type: openai_compat  # 或自定义 type
    base_url: https://api.my-llm.com/v1
    api_key: ${MY_LLM_KEY}
    model: my-model
```

## 4. 调试与测试

- 跑示例:`python examples/tools_demo.py`
- 跑你的工具的单测:写 `tests/test_my_plugin.py`
- 用 `openclaw tools list` 验证工具已注册
- 用 `openclaw security audit` 检查工具权限

## 5. 发布

1. 在 `pyproject.toml` 加 `openclaw.plugins = ["my_pkg = my_pkg.plugin_entry"]`(可选)
2. `pip install my-pkg`
3. 用户的 yaml 里 `tools.extras` 加 `my_pkg.tools.greet`

## 6. 常见坑

- ❌ **tool 返回不能被 JSON 序列化** → `to_jsonable` 兜底会 `repr()`,但你最好自己保证
- ❌ **tool name 含特殊字符** → name 必须是 `[a-z0-9_]+`,否则 Pydantic schema 校验失败
- ❌ **不写 docstring** → Agent 看不到 description,**会拒绝调用** (Phase 4 schema 校验)
- ❌ **provider api_key 漏配** → 启动期 `merge_with_env` 不抛错但 acomplete 调 OpenAI 时报 401(per-design 行为)

## 7. 完整项目模板

```bash
# 1) clone 仓库
git clone https://github.com/swolflins/openclaw-py-m3.git
cd openclaw-py-m3
# 2) 创建你的插件目录
mkdir -p my_plugins/weather
# 3) 参考 examples/skills/weather/ 写
# 4) 跑测试
pytest tests/ -q
```

参考 `examples/skills/{joke,system_status,weather}/` 三个完整示例。
