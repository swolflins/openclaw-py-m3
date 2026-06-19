"""Joke skill:从本地题库随机抽冷笑话。"""
import random

from openclaw.core.skills import SkillAPI
from openclaw.tools.registry import ToolCategory, ToolPermission

_JOKES = [
    "为什么程序员总爱穿黑衣服?因为 debug 没有彩色的 bug。",
    "一只蝙蝠飞进了一台电脑,它变成了什么?—— fan。",
    "Git 提交写 'fix typo',然后实际改了 200 行。",
    "把 'hello world' 翻译成中文,然后回译回英文,你会得到一个非常哲学的程序。",
    "两个程序员在聊天,一个说: '我昨天写了一个 0 bug 的程序。' 另一个说: '那是因为没有用户。'",
    "为什么 Python 工程师都喜欢养蛇?因为 finally 总是要执行的。",
]


def register(api: SkillAPI) -> None:
    @api.tool(
        name="random_joke",
        description="随机讲一个冷笑话(中文)。",
        category=ToolCategory.UTILITY,
        permission=ToolPermission.SAFE,
    )
    def random_joke() -> str:
        return random.choice(_JOKES)

    api.inject_prompt(
        "用户要笑话时,直接调 `random_joke` 工具,然后把笑话原样给用户。"
        "**不要**自己编,也不要在笑话前后加太多废话。"
    )
