"""Docker 沙箱工具(子包,可选依赖)。

- docker_exec:在临时容器里跑一段命令(自动拉镜像 + 清理)
- docker_run_python:在临时 Python 容器里跑脚本(沙箱最常用)
- docker_pull:预拉镜像

依赖:`pip install openclaw-py[all]` -> docker 包
"""
from __future__ import annotations


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
) -> None:
    """注册 docker_* 工具(都属 ADMIN 权限,需要审批)。

    总是注册四个工具,使得 agent 即使在 docker SDK 缺失时也能看到 schema;
    实际调用时再通过 _ensure_docker 报错。
    """
    if not _HAS_DOCKER:
        logger.info("docker_tools_skipped_register", reason="docker package not installed")

    @registry.tool(category=ToolCategory.SANDBOX, permission=ToolPermission.ADMIN)
    def docker_run_python(code: str, image: str = "") -> str:
        """在临时 Python 容器里跑一段代码并返回 stdout。code: Python 源码; image: 镜像,默认 python:3.11-slim。"""
        _ensure_docker()
        img = image or default_image
        client = docker.from_env()
        try:
            container = client.containers.run(
                img,
                command=["python", "-c", code],
                remove=True,
                mem_limit=mem_limit,
                network_disabled=network_disabled,
                stdout=True, stderr=True,
                detach=True,
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
        img = image or default_image
        client = docker.from_env()
        try:
            container = client.containers.run(
                img,
                command=["sh", "-c", command],
                remove=True, mem_limit=mem_limit,
                network_disabled=network_disabled,
                stdout=True, stderr=True, detach=True,
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
        client = docker.from_env()
        client.images.pull(image)
        return f"pulled {image}"

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
