"""Docker 沙箱工具(子包,可选依赖)。

- docker_exec:在临时容器里跑一段命令(自动拉镜像 + 清理)
- docker_run_python:在临时 Python 容器里跑脚本(沙箱最常用)
- docker_pull:预拉镜像

依赖:`pip install openclaw-py[all]` -> docker 包
"""
from __future__ import annotations

import re

from openclaw.core.errors import ToolError
from openclaw.core.logging import get_logger
from openclaw.tools.registry import ToolCategory, ToolPermission, ToolRegistry

logger = get_logger(__name__)

try:
    import docker  # type: ignore[import-not-found]
    from docker.errors import ImageNotFound

    _HAS_DOCKER = True
except Exception:  # pragma: no cover
    docker = None  # type: ignore[assignment]
    _HAS_DOCKER = False


# TOOL 优化:镜像白名单 — 防止 agent 拉恶意 / 不合规镜像
# 解析:取 ``repo[:tag]`` 的 repo 部分(忽略 registry),case-insensitive
_DEFAULT_ALLOWED_IMAGES: tuple[str, ...] = (
    "python:3.11-slim",
    "python:3.12-slim",
    "python:3.13-slim",
    "alpine:3.19",
    "alpine:3.20",
    "alpine:latest",
    "debian:12-slim",
    "ubuntu:24.04",
)

# 简易镜像名校验:防止注入 docker CLI 参数
_IMAGE_NAME_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9._\-/]{0,127}(?::[a-zA-Z0-9._\-]{1,64})?$"
)


def _check_image_allowed(image: str, allowed: tuple[str, ...]) -> None:
    """TOOL 优化:校验镜像名格式 + 是否在白名单。

    白名单匹配是**前缀匹配**:`python:3.11-slim` 允许 `python:3.11-slim` / `python:3.11`。
    """
    if not image or not _IMAGE_NAME_RE.match(image):
        raise ToolError(
            f"docker: 镜像名格式非法 {image!r}(只允许字母数字 + . _ - / :)"
        )
    img_norm = image.lower()
    for ok in allowed:
        ok_norm = ok.lower()
        if img_norm == ok_norm or img_norm.startswith(ok_norm + ":") or img_norm.startswith(ok_norm + "/"):
            return
    raise ToolError(
        f"docker: 镜像 {image!r} 不在白名单 {list(allowed)} 内"
    )


def _ensure_docker() -> None:
    if not _HAS_DOCKER:
        raise ToolError(
            "docker 未安装,运行 `pip install openclaw-py[all]` 获取 docker SDK"
        )


def register_docker_tools(
    registry: ToolRegistry,
    *,
    default_image: str = "python:3.11-slim",
    timeout: int = 60,
    mem_limit: str = "256m",
    network_disabled: bool = True,
    cpu_quota: float = 0.5,        # TOOL-1:1 CPU 周期内的占用比例(0.5 = 50%)
    cpu_period: int = 100_000,     # TOOL-1:CPU CFS 周期(us)
    pids_limit: int = 256,         # TOOL-1:进程数上限(防 fork 炸弹)
    read_only: bool = True,        # TOOL-1:根文件系统只读
    run_as_user: str = "65534",    # TOOL-1:以 nobody 运行(防 root 提权)
    cap_drop: tuple[str, ...] = ("ALL",),  # TOOL-1:丢掉所有 capabilities
    no_new_privileges: bool = True,  # TOOL-1:禁 SUID/SGID 提权
    allowed_images: tuple[str, ...] = _DEFAULT_ALLOWED_IMAGES,  # TOOL 优化:白名单
    enforce_allowlist: bool = True,  # TOOL 优化:False 可关闭(用于本地实验)
) -> None:
    """注册 docker_* 工具(都属 ADMIN 权限,需要审批)。

    总是注册四个工具,使得 agent 即使在 docker SDK 缺失时也能看到 schema;
    实际调用时再通过 _ensure_docker 报错。

    TOOL-1 安全加固:默认就以"低权限 + 资源受限 + 隔离"姿态运行容器,
    所有加固项可通过参数覆盖,但默认值是 safe-by-default。
    """
    if not _HAS_DOCKER:
        logger.info("docker_tools_skipped_register", reason="docker package not installed")

    def _hardened_kwargs() -> dict:
        """TOOL-1:所有容器统一使用的安全 + 资源限制选项。"""
        kw: dict = {
            "mem_limit": mem_limit,
            "network_disabled": network_disabled,
            "pids_limit": pids_limit,
            "read_only": read_only,
            "user": run_as_user,
            "cap_drop": list(cap_drop),
            "security_opt": ["no-new-privileges:true"] if no_new_privileges else [],
        }
        if cpu_quota and cpu_quota > 0:
            kw["cpu_quota"] = int(cpu_quota * cpu_period)
            kw["cpu_period"] = cpu_period
        return kw

    def _resolve_image(image: str) -> str:
        """TOOL 优化:白名单校验 + 默认值回退。"""
        img = image or default_image
        if enforce_allowlist:
            _check_image_allowed(img, allowed_images)
        return img

    @registry.tool(category=ToolCategory.SANDBOX, permission=ToolPermission.ADMIN)
    def docker_run_python(code: str, image: str = "") -> str:
        """在临时 Python 容器里跑一段代码并返回 stdout。code: Python 源码; image: 镜像,默认 python:3.11-slim。"""
        _ensure_docker()
        img = _resolve_image(image)
        client = docker.from_env()
        try:
            container = client.containers.run(
                img,
                command=["python", "-c", code],
                remove=True,
                stdout=True, stderr=True,
                detach=True,
                **_hardened_kwargs(),
            )
            try:
                result = container.wait(timeout=timeout)
                logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
                rc = result.get("StatusCode", -1)
                return f"[exit={rc}]\n{logs[:8000]}"
            finally:
                try:
                    container.remove(force=True)
                except Exception:
                    pass
        except ImageNotFound:
            logger.info("docker_pull_start", image=img)
            client.images.pull(img)
            return docker_run_python(code=code, image=img)

    @registry.tool(category=ToolCategory.SANDBOX, permission=ToolPermission.ADMIN)
    def docker_exec(command: str, image: str = "") -> str:
        """在临时容器里跑一条 shell 命令并返回结果。command: 完整命令; image: 镜像。"""
        _ensure_docker()
        img = _resolve_image(image)
        client = docker.from_env()
        try:
            container = client.containers.run(
                img,
                command=["sh", "-c", command],
                remove=True,
                stdout=True, stderr=True, detach=True,
                **_hardened_kwargs(),
            )
            try:
                result = container.wait(timeout=timeout)
                logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
                rc = result.get("StatusCode", -1)
                return f"[exit={rc}]\n{logs[:8000]}"
            finally:
                try:
                    container.remove(force=True)
                except Exception:
                    pass
        except ImageNotFound:
            client.images.pull(img)
            return docker_exec(command=command, image=img)

    @registry.tool(category=ToolCategory.SANDBOX, permission=ToolPermission.ADMIN)
    def docker_pull(image: str) -> str:
        """预拉取镜像。image: 镜像名如 python:3.12。"""
        _ensure_docker()
        # TOOL 优化:即使是显式 pull 也要走白名单
        img = _resolve_image(image)
        client = docker.from_env()
        client.images.pull(img)
        return f"pulled {img}"

    @registry.tool(category=ToolCategory.SANDBOX, permission=ToolPermission.READ)
    def docker_list_images() -> str:
        """列出本地已有 docker 镜像。"""
        if not _HAS_DOCKER:
            return "[error] docker 未安装,运行 `pip install openclaw-py[all]` 获取 docker SDK"
        client = docker.from_env()
        try:
            images = client.images.list()
        except Exception as e:
            return f"[error] 连接 docker daemon 失败: {e}"
        if not images:
            return "(no images)"
        return "\n".join(
            f"{','.join(i.tags) or '<none>':40s}  {i.attrs.get('Size', '?')}b"
            for i in images
        )
