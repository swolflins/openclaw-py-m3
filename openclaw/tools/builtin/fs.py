"""文件系统工具(子包)。

- read_file / write_file / list_dir / search_files / append_file / file_stat
- 安全:支持 root 限制(只允许在指定根目录下操作)
"""
from __future__ import annotations

import re
from pathlib import Path

from openclaw.core.logging import get_logger
from openclaw.tools.registry import ToolCategory, ToolPermission, ToolRegistry

logger = get_logger(__name__)


def register_fs_tools(
    registry: ToolRegistry,
    *,
    root: Path | str = ".",
    max_read_bytes: int = 200_000,
) -> None:
    """注册文件工具。root: 沙箱根目录(防止越权);max_read_bytes: 单次读取上限。"""
    base = Path(root).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)

    def _safe(p: Path | str) -> Path:
        target = (base / p).resolve() if not Path(p).is_absolute() else Path(p).resolve()
        # 允许 root 内的相对路径,不允许逃出 root
        if base in target.parents or target == base:
            return target
        # 严格:在 base 下
        try:
            target.relative_to(base)
        except ValueError:
            raise PermissionError(f"path {target} escapes root {base}")
        return target

    @registry.tool(category=ToolCategory.FS, permission=ToolPermission.READ)
    def read_file(path: str, max_bytes: int = 0) -> str:
        """读取文件内容。path: 相对或绝对路径(以 root 为基准); max_bytes: 0=使用默认上限。"""
        p = _safe(path)
        if not p.exists():
            return f"[error] file not found: {p}"
        cap = max_bytes or max_read_bytes
        text = p.read_text(encoding="utf-8", errors="replace")
        if len(text) > cap:
            text = text[:cap] + f"\n... [truncated, {len(text) - cap} chars omitted]"
        return text

    @registry.tool(category=ToolCategory.FS, permission=ToolPermission.WRITE)
    def write_file(path: str, content: str, overwrite: bool = False) -> str:
        """写入文件(默认拒绝覆盖)。path: 路径; content: 内容; overwrite: True 允许覆盖现有文件。"""
        p = _safe(path)
        if p.exists() and not overwrite:
            return f"[error] file exists, set overwrite=true to replace: {p}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        logger.info("fs_write", path=str(p), size=len(content), overwrite=overwrite)
        return f"wrote {len(content)} bytes to {p}"

    @registry.tool(category=ToolCategory.FS, permission=ToolPermission.WRITE)
    def append_file(path: str, content: str) -> str:
        """追加内容到文件末尾。path: 路径; content: 要追加的内容。"""
        p = _safe(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
        return f"appended {len(content)} bytes to {p}"

    @registry.tool(category=ToolCategory.FS, permission=ToolPermission.READ)
    def list_dir(path: str = ".", pattern: str = "*") -> str:
        """列出目录下的条目(glob 模式)。path: 相对 root 的目录; pattern: glob,默认 '*'。"""
        p = _safe(path)
        if not p.exists():
            return f"[error] dir not found: {p}"
        if not p.is_dir():
            return f"[error] not a directory: {p}"
        entries: list[str] = []
        for child in sorted(p.glob(pattern)):
            tag = "/" if child.is_dir() else ""
            size = "" if child.is_dir() else f"  {child.stat().st_size}b"
            entries.append(f"{child.name}{tag}{size}")
        return "\n".join(entries) if entries else "(empty)"

    @registry.tool(category=ToolCategory.FS, permission=ToolPermission.READ)
    def search_files(
        path: str = ".",
        pattern: str = "",
        regex: bool = False,
        max_results: int = 50,
    ) -> str:
        """在目录下搜索文件名或文件内容。path: 搜索根; pattern: 模式; regex: True 视为正则匹配文件内容(否则 glob 文件名); max_results: 上限。"""
        p = _safe(path)
        if not p.exists():
            return f"[error] dir not found: {p}"

        results: list[str] = []
        if not regex:
            for f in p.rglob(pattern or "*"):
                results.append(str(f.relative_to(p)))
                if len(results) >= max_results:
                    break
        else:
            try:
                rgx = re.compile(pattern)
            except re.error as e:
                return f"[error] bad regex: {e}"
            for f in p.rglob("*"):
                if not f.is_file():
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if rgx.search(line):
                        results.append(f"{f.relative_to(p)}:{i}: {line[:200]}")
                        if len(results) >= max_results:
                            break
                if len(results) >= max_results:
                    break
        return "\n".join(results) if results else "(no matches)"

    @registry.tool(category=ToolCategory.FS, permission=ToolPermission.READ)
    def file_stat(path: str) -> str:
        """获取文件/目录的元信息(size/mtime/mode)。path: 路径。"""
        p = _safe(path)
        if not p.exists():
            return f"[error] not found: {p}"
        st = p.stat()
        return (
            f"path: {p}\n"
            f"size: {st.st_size}\n"
            f"mtime: {st.st_mtime}\n"
            f"is_dir: {p.is_dir()}\n"
            f"is_file: {p.is_file()}\n"
            f"mode: {oct(st.st_mode)}"
        )
