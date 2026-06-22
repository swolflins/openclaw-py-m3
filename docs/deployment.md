# Deployment Guide (Phase 27 / M24)

本文档覆盖 6 个常见部署场景的清单 + 注意事项。

## 0. 前置:必设的 3 个 env

无论哪种部署,这 3 个 env 必须设:

```bash
# 1. 鉴权 token(必须,32+ 字符随机)
export OPENCLAW_GATEWAY_TOKEN=$(openssl rand -hex 32)

# 2. 生产模式(把 dev 默认关)
export OPENCLAW_GATEWAY_ENV=production

# 3. per-user 隔离需要 user_id 或 token_to_user 之一
# 单用户:
export OPENCLAW_GATEWAY_USER_ID=alice
# 多用户(JSON map):
export OPENCLAW_GATEWAY_TOKEN_TO_USER='{"tk1":"alice","tk2":"bob"}'
```

设错的后果:
- 缺 token + `production` → 启动期 RuntimeError 阻断(防未鉴权部署)
- 缺 token + dev 模式 → /v1/* 任何人可调(仅本地开发 OK)
- 缺 user_id + 多 token → per-user 隔离蒸发(token 轮换换 user_id)
- `production` + `OPENCLAW_GATEWAY_DEV=1` → RuntimeError 阻断(防"生产忘删 dev 开关")

---

## 1. Docker / docker-compose(推荐)

```bash
# 1) 准备 .env(包含 token,见 0 节)
cat > .env <<EOF
OPENCLAW_GATEWAY_TOKEN=$(openssl rand -hex 32)
EOF

# 2) 起服务
docker compose up -d

# 3) 验证
curl -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN" http://localhost:8080/v1/healthz
```

`docker-compose.yml` 已设:
- `OPENCLAW_GATEWAY_TOKEN=${OPENCLAW_GATEWAY_TOKEN:?...}` → 缺 token 时启动期**报错**(不静默放过)
- `OPENCLAW_GATEWAY_HOST=0.0.0.0` → 容器内监听所有接口
- `OPENCLAW_GATEWAY_PORT=8080` → 与外部端口映射对齐

---

## 2. systemd(裸机 / 虚拟机)

```ini
# /etc/systemd/system/openclaw.service
[Unit]
Description=OpenClaw Agent Gateway
After=network.target

[Service]
Type=simple
User=openclaw
WorkingDirectory=/opt/openclaw
Environment="OPENCLAW_GATEWAY_TOKEN=__SET_ME__"
Environment="OPENCLAW_GATEWAY_ENV=production"
Environment="OPENCLAW_GATEWAY_USER_ID=alice"
ExecStart=/opt/openclaw/.venv/bin/python -m openclaw.cli gateway start --host 127.0.0.1 --port 8080
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now openclaw
systemctl status openclaw
```

---

## 3. 反向代理(Nginx)

**只**接受 `127.0.0.1` 流量(防外网绕过鉴权):

```nginx
server {
    listen 443 ssl http2;
    server_name openclaw.example.com;
    ssl_certificate /etc/letsencrypt/live/openclaw.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/openclaw.example.com/privkey.pem;

    # 可信代理:这里告诉 gateway "X-Forwarded-For 是真实的"
    # (gateway 端需配 OPENCLAW_GATEWAY_TRUSTED_PROXY=1)
    real_ip_header X-Forwarded-For;
    set_real_ip_from 127.0.0.1;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # SSE 流式响应必备
        proxy_buffering off;
        proxy_read_timeout 600s;
    }
}
```

gateway 端配:

```bash
export OPENCLAW_GATEWAY_TRUSTED_PROXY=1  # 信任 X-Forwarded-For(让限流 key 走真实 IP)
export OPENCLAW_GATEWAY_HOST=127.0.0.1    # 仍绑 loopback(nginx 转发)
```

**关键**:不设 `TRUSTED_PROXY=1` 时,限流 key 走 `client.host`(所有 nginx 流量共享 `127.0.0.1`,
所有用户被同一桶限流)。设了之后,每个真实客户端 IP 独立限流。

---

## 4. TLS / mTLS

openclaw-py-m3 自身**不**做 TLS(交给前端的 Nginx / Caddy / Cloudflare)。
如需 mTLS(client cert 鉴权),在 Nginx 端配 `ssl_verify_client on`。

---

## 5. 监控

Prometheus 抓取 `/metrics`:

```yaml
# ops/prometheus.yml
scrape_configs:
  - job_name: openclaw
    static_configs:
      - targets: ['localhost:8080']
    metrics_path: /metrics
```

关键指标:
- `openclaw_chat_total` / `openclaw_chat_errors_total`
- `openclaw_gateway_auth_rejected_total` (SIEM 拉取检测暴力破解)
- `openclaw_uptime_seconds` / `openclaw_agent_attached`

Loki/Grafana 看结构化日志(structlog JSON)。

---

## 6. 备份

需要备份的目录(默认在 `~/.openclaw/`):

```
~/.openclaw/
├── openclaw.yaml          # 主配置
├── channels/creds.json    # 渠道凭据(敏感!)
├── journal/               # Agent journal(SOUL 反思)
├── memory/long/           # 向量数据库(看 backend:chroma → chroma.sqlite3)
└── soul/                  # SOUL.md 长期记忆
```

**凭据 + journal 是金标准**,建议:
- 全量备份: `tar czf openclaw-backup.tgz ~/.openclaw/`
- 加密: `gpg -c openclaw-backup.tgz`
- 异地: rsync 到异地 / S3 glacier

**切勿**把 `creds.json` 提交到 git(`.gitignore` 已忽略 `~/.openclaw/`,但保险起见 `git status` 检查)。

---

## 7. 升级 / 回滚

```bash
# 1) 拉新版本
git pull origin main

# 2) 跑测试
make ci-check

# 3) 重启
docker compose pull && docker compose up -d
# 或
systemctl restart openclaw

# 4) 回滚(如果挂了)
git checkout <last-good-tag>
systemctl restart openclaw
```

看 `CHANGELOG.md` 的"Notes for Upgraders"段,确认 BC-breaking 变更。
