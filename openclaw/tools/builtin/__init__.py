"""内置工具子包。

通过 register_builtin_tools(registry) 一次性注册:
- UTILITY: get_current_time / format_time / parse_time / timezone_convert / date_diff / calculator / echo
- FS:      read_file / write_file / append_file / list_dir / search_files / file_stat
- SHELL:   shell_exec
- HTTP:    http_get / http_post / http_request
- CRON:    cron_add / cron_list / cron_remove
- SANDBOX: docker_run_python / docker_exec / docker_pull / docker_list_images
- BROWSER: browse_url / browser_extract_text / browser_close(可选,需 playwright)

参数 include / exclude 用于精确控制。
"""
from openclaw.core.logging import get_logger
from openclaw.tools.registry import ToolCategory, ToolRegistry

from openclaw.tools.builtin.cron import CronManager, get_cron_manager, register_cron_tools
from openclaw.tools.builtin.datetime import register_datetime_tools
from openclaw.tools.builtin.docker import register_docker_tools
from openclaw.tools.builtin.fs import register_fs_tools
from openclaw.tools.builtin.http import register_http_tools
from openclaw.tools.builtin.shell import register_shell_tools

logger = get_logger(__name__)


def register_builtin_tools(
    registry: ToolRegistry,
    *,
    fs_root: str = ".",
    shell_allowed: list[str] | None = None,
    shell_default_cwd: str = ".",
    http_allowed_hosts: list[str] | None = None,
    browser_allowed_domains: list[str] | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> None:
    """注册所有内置工具。"""
    before: set[str] = {t.name for t in registry.list_tools()}

    register_datetime_tools(registry)
    register_fs_tools(registry, root=fs_root)
    register_shell_tools(
        registry, default_cwd=shell_default_cwd, allowed=shell_allowed,
    )
    register_http_tools(registry, allowed_hosts=http_allowed_hosts)
    register_cron_tools(registry)

    # Phase 36: 联网工具(get_weather / web_search / web_fetch,免 API key)
    from openclaw.tools.builtin.web import register_web_tools
    try:
        register_web_tools(registry)
    except Exception as e:  # pragma: no cover
        logger.info("web_tools_skipped", reason=str(e))

    try:
        register_docker_tools(registry)
    except Exception as e:  # pragma: no cover
        logger.info("docker_tools_skipped", reason=str(e))

    # playwright 可选 — 没装时跳过(不阻断)
    try:
        from openclaw.tools.builtin.playwright_tool import register_browser_tools
        register_browser_tools(
            registry, allowed_domains=browser_allowed_domains,
        )
    except ImportError as e:
        logger.info("browser_tools_skipped", reason=str(e))

    # 兼容老 demo
    @registry.tool(category=ToolCategory.UTILITY, permission="safe")
    def calculator(expression: str) -> str:
        """计算一个安全的数学表达式(仅支持数字与 + - * / ( ) . ** %)。"""
        import ast
        import operator

        bin_ops = {
            ast.Add: operator.add, ast.Sub: operator.sub,
            ast.Mult: operator.mul, ast.Div: operator.truediv,
            ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
            ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
        }

        def _eval(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                return node.value
            if isinstance(node, ast.BinOp) and type(node.op) in bin_ops:
                return bin_ops[type(node.op)](_eval(node.left), _eval(node.right))
            if isinstance(node, ast.UnaryOp) and type(node.op) in bin_ops:
                return bin_ops[type(node.op)](_eval(node.operand))
            raise ValueError(f"unsupported expression node: {ast.dump(node)}")

        tree = ast.parse(expression, mode="eval")
        return str(_eval(tree.body))

    @registry.tool(category=ToolCategory.UTILITY, permission="safe")
    def echo(message: str) -> str:
        """把 message 原样返回,用来演示工具调用。"""
        return message

    if include is not None or exclude is not None:
        keep: set[str] = set()
        for t in registry.list_tools():
            if t.name in before:
                continue
            if include is not None and t.name not in include:
                continue
            if exclude is not None and t.name in exclude:
                continue
            keep.add(t.name)
        to_remove = [
            t.name for t in registry.list_tools()
            if t.name not in before and t.name not in keep
        ]
        for n in to_remove:
            registry._tools.pop(n, None)  # type: ignore[attr-defined]
        logger.info(
            "builtin_tools_filtered", kept=sorted(keep), removed=to_remove,
        )


__all__ = [
    "CronManager",
    "get_cron_manager",
    "register_cron_tools",
    "register_datetime_tools",
    "register_docker_tools",
    "register_fs_tools",
    "register_http_tools",
    "register_shell_tools",
    "register_builtin_tools",
]
