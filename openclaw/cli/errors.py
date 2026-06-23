"""CLI 统一错误处理。

约定 exit code:
- 0   成功
- 1   未知错误
- 2   配置错误(ConfigError)
- 3   依赖缺失
- 4   网络错误
- 5   未找到(not found)
- 79  Provider 错误(对齐 sysexits.h 后 CONFIG 子空间)
- 80  Plugin 错误
- 81  工具校验错误
"""
from __future__ import annotations

import difflib
import sys
import traceback
from typing import Optional

# exit code 常量
EXIT_OK = 0
EXIT_UNKNOWN = 1
EXIT_CONFIG = 2
EXIT_DEPENDENCY = 3
EXIT_NETWORK = 4
EXIT_NOT_FOUND = 5
EXIT_PROVIDER = 79
EXIT_PLUGIN = 80
EXIT_TOOL_VALIDATION = 81


class CLIError(Exception):
    """携带 exit code 的 CLI 异常。

    捕获后应打印 ``message`` 到 stderr 并以 ``exit_code`` 退出。
    """

    def __init__(self, message: str, *, exit_code: int = EXIT_UNKNOWN, hint: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.hint = hint


def suggest_commands(typed: str, available: list[str], n: int = 3) -> list[str]:
    """对输错的命令名做相似度建议(difflib)。"""
    return difflib.get_close_matches(typed, available, n=n, cutoff=0.4)


def _openclaw_error_exit_code(exc: Exception) -> int:
    """把 openclaw 内部异常族映射到 exit code。"""
    name = type(exc).__name__
    if name == "ConfigError":
        return EXIT_CONFIG
    if name == "ProviderError":
        return EXIT_PROVIDER
    if name == "PluginError":
        return EXIT_PLUGIN
    if name == "ToolValidationError":
        return EXIT_TOOL_VALIDATION
    return EXIT_UNKNOWN


def handle_error(exc: Exception, *, verbose: bool = False) -> int:
    """统一错误处理:打印到 stderr,返回 exit code。

    本函数不主动 sys.exit,由调用方决定(便于测试)。
    """
    if isinstance(exc, CLIError):
        print(f"错误: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"提示: {exc.hint}", file=sys.stderr)
        return exc.exit_code

    # openclaw 内部异常族
    name = type(exc).__name__
    if name in {"ConfigError", "ProviderError", "PluginError", "ToolValidationError"}:
        print(f"错误: {exc}", file=sys.stderr)
        return _openclaw_error_exit_code(exc)

    # 未知异常
    print(f"错误: {exc}", file=sys.stderr)
    if verbose:
        traceback.print_exc(file=sys.stderr)
    else:
        print("提示: 加 --verbose 查看完整堆栈", file=sys.stderr)
    return EXIT_UNKNOWN
