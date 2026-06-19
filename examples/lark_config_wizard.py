"""Phase 10 飞书后台配置向导。

一行命令做完"我现在卡在哪一步"诊断:
  LARK_APP_ID=cli_xxx LARK_APP_SECRET=xxx python examples/lark_config_wizard.py

输出:
  1) 5 端点完整探针(token / bot / app / event / chats)
  2) 每个端点的状态 + 自动建议
  3) 后台"操作清单"(按顺序执行就能把 bot 跑通)
  4) 错误码 → 中文说明 + 修复路径(查表)

不依赖 lark-oapi / LLM key,只需要有效的 app_id + app_secret。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openclaw.channels.lark_wizard import probe_all, render_report  # noqa: E402

APP_ID = os.environ.get("LARK_APP_ID", "")
APP_SECRET = os.environ.get("LARK_APP_SECRET", "")


def main() -> None:
    print(f"\n  LARK_APP_ID     = {APP_ID or '(unset)'}")
    print(f"  LARK_APP_SECRET = {'set' if APP_SECRET else '(unset)'}")
    if not APP_ID or not APP_SECRET:
        print("\n  ❌ 请先设置 LARK_APP_ID / LARK_APP_SECRET 环境变量")
        print("     export LARK_APP_ID=cli_xxx")
        print("     export LARK_APP_SECRET=xxx")
        sys.exit(1)

    report = asyncio.run(probe_all(APP_ID, APP_SECRET))
    print(render_report(report))

    # JSON 也输出到 /tmp,方便给后续脚本解析
    out = Path("/tmp/lark_wizard_report.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(f"  (完整 JSON 报告:{out})")


if __name__ == "__main__":
    main()
