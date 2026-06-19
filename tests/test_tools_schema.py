"""测试:工具 JSON Schema 生成。"""
from openclaw.tools.registry import ToolRegistry


def test_basic_types():
    reg = ToolRegistry()

    @reg.tool
    def search(query: str, top_k: int = 3, verbose: bool = False) -> str:
        """搜索函数。

        query: 搜索关键词
        top_k: 返回数量
        verbose: 是否输出调试信息
        """
        return ""

    spec = reg.get("search").to_spec().to_openai_tool()
    fn = spec["function"]
    assert fn["name"] == "search"
    assert "搜索函数" in fn["description"]
    props = fn["parameters"]["properties"]
    assert props["query"]["type"] == "string"
    assert props["top_k"]["type"] == "integer"
    assert props["top_k"]["default"] == 3
    assert props["verbose"]["type"] == "boolean"
    assert set(fn["parameters"]["required"]) == {"query"}


def test_no_required_when_all_have_defaults():
    reg = ToolRegistry()

    @reg.tool
    def f(a: int = 1, b: str = "x") -> str:
        return ""

    assert reg.get("f").to_spec().to_openai_tool()["function"]["parameters"].get("required", []) == []
