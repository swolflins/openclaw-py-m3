"""Phase 18 测试:CI docker build 修复回归。

覆盖:
- Dockerfile 移除有 bug 的 ARG PYTHON_VERSION 注入
- site-packages 路径使用字面量 python3.11(不依赖 patch 号)
- 关键指令(ENTRYPOINT / CMD / HEALTHCHECK)仍存在
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"


def test_dockerfile_exists():
    assert DOCKERFILE.is_file(), "Dockerfile 缺失"


def test_dockerfile_no_arg_python_version_bug():
    """旧 bug:Dockerfile 用 ${PYTHON_VERSION} 拼 site-packages,会被 buildx
    解析为 python3.11.15 而非 python3.11,造成 COPY 失败。
    """
    content = DOCKERFILE.read_text(encoding="utf-8")
    # 去掉注释,只看运行指令
    import re
    no_comments = re.sub(r"#[^\n]*", "", content)
    # 不应再有 ARG PYTHON_VERSION
    assert "ARG PYTHON_VERSION" not in no_comments, (
        "Dockerfile 还有 ARG PYTHON_VERSION,会触发 'site-packages: not found' 错误"
    )
    # 不应再用 ${PYTHON_VERSION} 拼路径
    assert "python${PYTHON_VERSION}" not in no_comments, (
        "Dockerfile 还在用 python${PYTHON_VERSION} 拼路径"
    )


def test_dockerfile_uses_literal_site_packages_path():
    """修复后:用字面量 /usr/local/lib/python3.11/site-packages 复制 site-packages。"""
    content = DOCKERFILE.read_text(encoding="utf-8")
    # 必须出现字面量路径(源和目标)
    assert content.count("/usr/local/lib/python3.11/site-packages") >= 2, (
        "site-packages 路径应出现至少 2 次(COPY 的 src 和 dest)"
    )


def test_dockerfile_has_two_stages():
    """应有 builder + runtime 两个 stage。

    Phase 28 / M18 修复:基镜像加 ``@sha256:...`` digest 锁定,
    但 ``FROM python:3.11-slim AS ...`` 的 AS 段必须仍是字面量,故
    ``FROM python:3.11-slim`` 前缀的 FROM 必须出现 2 次(digest 可选)。
    """
    content = DOCKERFILE.read_text(encoding="utf-8")
    assert "AS builder" in content
    assert "AS runtime" in content
    # M18 修复:digest 锁定 (e.g. @sha256:...) — 用前缀匹配更稳
    import re
    from_matches = re.findall(r"^FROM\s+python:3\.11-slim(?:@sha256:[a-f0-9]+)?\s+AS", content, re.MULTILINE)
    assert len(from_matches) == 2, (
        f"两个 FROM 都必须用字面量 python:3.11-slim(可选 @sha256 digest),"
        f"不能用 ${{PYTHON_VERSION}}。实匹配 {from_matches!r}"
    )


def test_dockerfile_runtime_artifacts_present():
    """关键运行期 artifact 必须保留。"""
    content = DOCKERFILE.read_text(encoding="utf-8")
    assert "uvicorn" in content and "openclaw.gateway.app:app" in content
    assert "tini" in content, "tini 信号转发"
    assert "HEALTHCHECK" in content
    assert "USER openclaw" in content, "non-root user"
    assert "8080" in content, "端口 8080"
    assert "VOLUME [\"/data\"]" in content
