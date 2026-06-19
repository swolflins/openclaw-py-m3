"""OpenClaw Gateway (Phase 8)。

一个统一的 HTTP 入口,把已有的能力暴露出去:

- POST /v1/chat /v1/chat/stream  — 跟 AgentLoop 对话(单轮 / SSE)
- GET  /v1/sessions              — 列出/查看/清除 session
- GET  /v1/memory/*              — 短期/长期记忆读写 + SOUL 预览
- GET  /v1/tools                 — 列出/调用工具
- GET  /v1/skills                — 列出/重载 skills
- GET  /v1/channels              — 列出/启停 channel
- GET  /healthz /readyz /metrics — 健康检查

Web UI 在 / (单页:session 列表 + 聊天 + 工具抽屉)。

启动:
    uvicorn openclaw.gateway.app:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations
