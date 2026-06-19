---
name: system_status
version: 0.1.0
description: 查本机状态(CPU/内存/磁盘/启动时间),不调外部 API
triggers: [系统状态, sysstatus, 健康检查, 内存占用, 磁盘剩余]
---

# System Status Skill

提供 `system_status` 工具,使用 `psutil` 读本机 CPU/内存/磁盘/启动时间。
如未装 psutil,自动用 `os.popen` 的 fallback(精度略低)。
