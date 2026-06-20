"""Phase 19 续:CI Windows job 必须用 --no-cov(避免触发 cov-fail-under)。

CI test-windows job 之前在 pyproject.toml 写死 --cov-fail-under=70 的情况下,
跑 phase 19 + phase 4(25 个)只能覆盖 ~12% 代码,触发 'Required test coverage
of 70% not reached' 而失败。

锁定:Windows job 必须传 --no-cov(或等价机制)关闭覆盖率门禁。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
YML = ROOT / ".github/workflows/ci.yml"


@pytest.fixture
def yml() -> str:
    return YML.read_text(encoding="utf-8")


def test_yml_exists():
    assert YML.is_file(), ".github/workflows/ci.yml 缺失"


def test_windows_job_uses_no_cov(yml: str):
    """Windows job 启动 pytest 时必须用 --no-cov(或其他等价手段)关闭覆盖率门禁。"""
    # test-windows job 内必须出现 --no-cov(或者 -o addopts=)
    # 抓 test-windows job 整段(steps 列表内)
    m = re.search(
        r"test-windows:.*?steps:\n(.*?)(?=\n  [a-zA-Z]|\n\Z)",
        yml, re.DOTALL,
    )
    assert m is not None, "找不到 test-windows job"
    job_block = m.group(1)
    has_no_cov = "--no-cov" in job_block
    has_addopts_empty = re.search(r"-o\s+addopts\s*=\s*['\"]", job_block) is not None
    assert has_no_cov or has_addopts_empty, (
        f"test-windows job 必须用 --no-cov 或 -o addopts= 关闭覆盖率门禁,实际是:\n{job_block[:400]}"
    )


def test_windows_job_only_runs_subset(yml: str):
    """Windows job 应只跑 phase 19 + phase 4 关键测试(避开 chromadb 下载慢)。"""
    m = re.search(
        r"test-windows:.*?steps:\n(.*?)(?=\n  [a-zA-Z]|\n\Z)",
        yml, re.DOTALL,
    )
    assert m is not None
    job_block = m.group(1)
    # 显式列了哪些 phase(不要走默认 tests/ 全跑)
    assert "tests/test_phase19_windows_shell.py" in job_block, (
        "Windows job 应只跑 phase 19 关键测试,不要全跑 tests/"
    )
    # 不能裸写 'pytest' 不带参数(会跑整个 testpaths)
    bare = re.search(r"^\s*pytest\s*$", job_block, re.MULTILINE)
    assert bare is None, "Windows job 不应裸跑 pytest(会触发全量 tests/)"


def test_ubuntu_test_job_still_uses_coverage_gate(yml: str):
    """Ubuntu test job 必须保留 --cov-fail-under=70 的覆盖率门禁。"""
    # 抓 '  test:' 之后到下一个 '  <name>:' 之前
    m = re.search(
        r"^  test:\s*\n(.*?)(?=\n  [a-zA-Z]|\Z)",
        yml, re.DOTALL | re.MULTILINE,
    )
    assert m is not None, "找不到 ubuntu test job"
    job_block = m.group(1)
    assert "--cov-fail-under" in job_block, (
        "Ubuntu test job 必须保留 --cov-fail-under 门禁"
    )
