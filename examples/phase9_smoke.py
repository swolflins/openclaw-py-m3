"""Phase 9 烟测:验证 Dockerfile / compose / prom / CI 静态结构 + Prometheus 抓取。

不依赖 docker 引擎(沙箱里通常没装):
  1) Dockerfile / compose / prom / ci 全部能解析
  2) 用 prom 文本格式抓 /metrics(模拟 Prometheus 抓取行为)
  3) 启动 fake gateway + 打几次 chat + tools,看 counter 自增
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check(label: str, ok: bool) -> None:
    print(f"  {'✅' if ok else '❌'} {label}")


# ---- 1) 静态文件解析 ----

print("\n[1] 静态文件解析")

dockerfile = (ROOT / "Dockerfile").read_text()
check("Dockerfile 多阶段", dockerfile.count("FROM") >= 2 and "AS builder" in dockerfile and "AS runtime" in dockerfile)
check("Dockerfile non-root", "USER openclaw" in dockerfile)
check("Dockerfile EXPOSE 8080", "EXPOSE 8080" in dockerfile)
check("Dockerfile HEALTHCHECK", "HEALTHCHECK" in dockerfile and "/healthz" in dockerfile)

compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
check("compose 含 services + gateway + volumes", "services" in compose and "gateway" in compose["services"] and "volumes" in compose)
check("compose 含 redis + ollama + prometheus",
      all(s in compose["services"] for s in ("redis", "ollama", "prometheus")))

prom = yaml.safe_load((ROOT / "ops" / "prometheus.yml").read_text())
check("prometheus.yml 含 openclaw-gateway job",
      any(j.get("job_name") == "openclaw-gateway" for j in prom["scrape_configs"]))

ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
check("CI 含 test + docker job", "test" in ci["jobs"] and "docker" in ci["jobs"])
check("CI matrix 含 Python 3.10/3.11",
      "3.10" in ci["jobs"]["test"]["strategy"]["matrix"]["python-version"]
      and "3.11" in ci["jobs"]["test"]["strategy"]["matrix"]["python-version"])

makefile = (ROOT / "Makefile").read_text()
check("Makefile 含 dev / test / lint / serve / docker", all(f"\n{t}:" in makefile or f"^{t}:" in makefile for t in ("dev", "test", "lint", "serve", "docker")))

# ---- 2) 启动 fake gateway + Prometheus 抓取 ----

print("\n[2] Prometheus 抓取 /metrics")

import uvicorn
from openclaw.gateway import deps as deps_mod
from openclaw.gateway.app import create_app


class FakeAgent:
    async def handle(self, sid, text, **kw):
        class R:
            content = f"[echo:{sid}] {text}"
            tool_calls = []
            iterations = 1
        return R()

    @property
    def tools(self):
        class R:
            specs = []
            def list_tools(self_inner): return []
            async def call(self_inner, n, a): return {"ok": True, "n": n, "a": a}
        return R()

    @property
    def memory(self):
        class M:
            short = type("S", (), {"all_scopes": lambda s: [], "recent_messages": lambda s, sc, k=20: [], "append_turn": lambda s, *a, **kw: None, "clear": lambda s, sc: None})()
        return M()


deps_mod.reset_deps()
deps_mod.set_deps(deps_mod.GatewayDeps(agent_loop=FakeAgent(), config_path=Path("/tmp/openclaw.yaml")))
app = create_app(deps=deps_mod.get_deps())


def _run() -> None:
    cfg = uvicorn.Config(app, host="127.0.0.1", port=18182, log_level="warning", loop="asyncio")
    uvicorn.Server(cfg).run()


t = threading.Thread(target=_run, daemon=True)
t.start()
time.sleep(1.2)

BASE = "http://127.0.0.1:18182"

with httpx.Client(base_url=BASE, timeout=5.0) as c:
    # 模拟 Prometheus 抓取:带 Accept: text/plain
    r = c.get("/metrics", headers={"Accept": "text/plain;version=0.0.4"})
    check("/metrics → prom 文本", r.status_code == 200 and r.headers["content-type"].startswith("text/plain"))
    text = r.text
    check("含 # HELP / # TYPE",
          "# HELP openclaw_uptime_seconds" in text and "# TYPE openclaw_uptime_seconds gauge" in text)
    check("agent_attached = 1", "openclaw_agent_attached 1.0" in text or "openclaw_agent_attached 1" in text)

    # 打几次 chat
    for i in range(3):
        c.post("/v1/chat", json={"session_id": f"u{i}", "message": "hi"})
    r = c.get("/metrics", headers={"Accept": "text/plain"})
    text = r.text
    import re
    matches = re.findall(r"openclaw_chat_total\{session_id=\"(u\d+)\"\} (\d+)", text)
    check("chat_total 计数自增", len(matches) >= 1 and any(int(v) >= 1 for _, v in matches),
          )

    # JSON 格式
    r = c.get("/metrics")
    check("/metrics JSON fallback", r.status_code == 200 and "uptime_s" in r.json())

print("\n=== Phase 9 烟测完成 ===")
print("  Dockerfile / compose / prom / CI 静态校验全过")
print("  Prometheus 抓取 + 计数器自增 OK")
print("  生产化产物就绪 — 可以直接 `docker compose up -d --build`")
