"""Weather skill:把 weather_query 工具注册到 skill 的 tool registry。

演示:
- 装饰器式 @api.tool 注册
- @api.inject_prompt() 往 system_prompt 追加 skill 提示
"""
from openclaw.core.skills import SkillAPI
from openclaw.tools.registry import ToolCategory, ToolPermission


# 模拟天气数据(实际可调 wttr.in / openweather)
_MOCK = {
    "beijing":  {"temp_c": 28, "cond": "晴", "humidity": 45},
    "shanghai": {"temp_c": 31, "cond": "多云", "humidity": 70},
    "hangzhou": {"temp_c": 30, "cond": "小雨", "humidity": 80},
    "tokyo":    {"temp_c": 26, "cond": "阴", "humidity": 60},
}


def register(api: SkillAPI) -> None:
    @api.tool(
        name="weather_query",
        description="查某城市当前天气(模拟数据)。city: 城市英文名小写。",
        category=ToolCategory.UTILITY,
        permission=ToolPermission.SAFE,
    )
    def weather_query(city: str) -> str:
        c = city.strip().lower()
        if c not in _MOCK:
            return f"(无 {city} 数据,仅支持 {', '.join(_MOCK.keys())})"
        m = _MOCK[c]
        return f"{city}: {m['cond']} {m['temp_c']}°C 湿度 {m['humidity']}%"

    api.inject_prompt(
        "当用户问天气时,**必须**先调 `weather_query` 工具,再把结果用中文总结。"
        "如果用户没说城市,直接回 '请告诉我你想查哪个城市(英文名)'。"
    )
