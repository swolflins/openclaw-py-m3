# Makefile — openclaw-py
#
# 一键命令:
#   make help       # 列出所有目标
#   make dev        # 本地装开发依赖
#   make test       # 跑测试
#   make lint       # ruff
#   make serve      # 启 gateway(http://127.0.0.1:8080)
#   make cli        # 启 CLI 交互
#   make docker     # 构镜像
#   make compose    # docker compose up
#   make smoke      # 跑所有 phase 的烟测
#   make clean      # 清缓存

PY     ?= python
PIP    ?= $(PY) -m pip
PYTEST ?= $(PY) -m pytest
RUFF   ?= $(PY) -m ruff

PHASE_SMOKES = p4_agnes_smoke phase5_smoke phase6_smoke phase7_smoke phase8_smoke

.PHONY: help dev install test test-fast lint fmt serve cli docker build compose \
        up down logs smoke smoke-p7 smoke-p8 clean distclean

help:  ## 列出所有目标
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

dev:  ## 装开发 + 全部可选依赖
	$(PIP) install -e ".[dev,all]"

install: dev

test:  ## 跑全部测试
	$(PYTEST) tests/ -v

test-fast:  ## 跑测试(不显示 traceback)
	$(PYTEST) tests/ -q --tb=no

lint:  ## ruff check
	$(RUFF) check openclaw/ tests/ examples/

fmt:  ## ruff 自动 fix
	$(RUFF) check --fix openclaw/ tests/ examples/

serve:  ## 启 gateway(http://127.0.0.1:8080/ui/)
	$(PY) -m uvicorn openclaw.gateway.app:app --reload --host 0.0.0.0 --port 8080

cli:  ## 启 CLI 交互
	$(PY) -m openclaw.cli

docker:  ## 构 docker 镜像
	docker build -t openclaw-py:dev -t openclaw-py:latest .

build: docker

compose:  ## docker compose up(后台)
	docker compose up -d --build

up: compose

down:  ## docker compose down
	docker compose down

logs:  ## docker compose logs
	docker compose logs -f --tail=100

smoke:  ## 跑全部 phase 烟测(需要对应 env)
	@for p in $(PHASE_SMOKES); do \
		echo "=== $$p ==="; \
		$(PY) examples/$$p.py 2>&1 | tail -8 || echo "  (skipped or failed)"; \
	done

smoke-p7:  ## 跑 phase 7 烟测
	$(PY) examples/phase7_smoke.py

smoke-p8:  ## 跑 phase 8 烟测(启 uvicorn + curl)
	$(PY) examples/phase8_smoke.py

clean:  ## 清缓存 / 临时文件
	rm -rf .pytest_cache .ruff_cache .mypy_cache .pyright_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name '.test_*' -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name '.openclaw_memory' -exec rm -rf {} + 2>/dev/null || true

distclean: clean  ## clean + 删 egg-info / dist
	rm -rf *.egg-info dist build
