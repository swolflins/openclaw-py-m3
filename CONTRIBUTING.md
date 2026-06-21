# Contributing / Development Workflow

## 规则：每次完成功能开发 / 问题修复都必须跑 CI 验证

**Why:** 历史经验：Phase 22 推送时本地全过，但 CI 的 `pip install -e .[all]` 多装了 playwright
Python 包，而 chromium 二进制没装 → 2 个 e2e test 在本地 SKIP（因为我本地有 chromium），
在 CI 里反而 FAIL。用户收到 GitHub 邮件通知才发现。**本地全过 ≠ CI 全过**。

**Rule (must):** 每次完成一次功能开发 / 问题修复后,**在 push 到 origin 之前**,本地必须跑过:

```bash
make ci-check        # ruff + pytest 70% 门禁全套(模拟 CI ubuntu job)
```

且 `git push` 之后**主动去 GitHub Actions 页面确认 6/6 job 全绿**:
- pip-audit
- ruff + pytest (3.10) / (3.11) / (3.12)
- ruff + pytest (windows)
- docker build

如果 CI 红,**立刻修并 push 修复 commit,不要发 "已修" 的话术先发,等绿了再说话**。
GitHub 邮件通知是**对外公开的历史**,红一次就被记录一次,无法靠后续的绿 commit 抹除。
唯一的解法是**push 修好的 commit**,然后等 CI 跑出新的绿 run。

## 快速命令

```bash
make ci-check        # ruff + pytest (与 CI ubuntu job 等效)
make ruff            # 只跑 ruff check
make test            # 只跑 pytest (无 coverage gate)
```

## 常见 CI 失败原因 → 快速排查

| 症状 | 原因 | 修法 |
|---|---|---|
| `Executable doesn't exist at .../chromium_headless_shell-...` | CI 装了 playwright 包但没装 chromium binary | 测试用 `pytest.importorskip` 或运行时 check `executable_path` 是否存在再 skip(参考 `test_phase21_playwright.py`) |
| `Required test coverage of 70% not reached` | 新代码拖低覆盖率 | 加新单测;或在文件顶部加 `# pragma: no cover`(谨慎,只对 error path) |
| `ruff F401 'xxx' imported but unused` | 删代码忘删 import | `ruff check --fix` |
| windows job 单独 fail | Windows shlex / path 解析差异 | 参考 `phase 19` 修复模式:`sys.platform == "win32"` 时切 `posix=False` |
| docker build `site-packages: not found` | `ARG PYTHON_VERSION` 与 `ENV PYTHON_VERSION` 冲突 | 改用字面量 `python3.11` (phase 18 修复) |

## 别做

- ❌ **不要只跑本地 `pytest` 就 push** — 本地与 CI 环境差异会导致"本地过 CI 挂"
- ❌ **不要相信 `git log --oneline` 没有红就认为 CI 没问题** — 邮件通知的是历史 run
- ❌ **不要堆 "fix CI" 的 empty commit** — 修根本原因,而非加 `git commit --allow-empty`
