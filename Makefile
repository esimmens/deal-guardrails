SHELL := /bin/bash
ENVFILE := $(firstword $(wildcard .env env))
COMPOSE := docker compose --env-file $(ENVFILE)
PSQL_ADMIN := $(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U dg_admin -d dg

.PHONY: up down nuke infra reset seed test evals report verify-audit push-agent push-tests tunnel tamper-demo smoke psql logs

up:            ## build and start everything, wait for health
	$(COMPOSE) up -d --build
	@$(COMPOSE) ps

infra:         ## start only postgres, n8n, caddy, ngrok (no service build)
	$(COMPOSE) up -d postgres n8n caddy ngrok
	@$(COMPOSE) ps

down:
	$(COMPOSE) down

nuke:          ## destroy volumes too (re-runs migrations on next up)
	$(COMPOSE) down -v

reset:         ## truncate working tables (admin only) and reseed
	$(PSQL_ADMIN) -f /db/reset.sql
	uv run python db/seed/seed.py

seed:
	uv run python db/seed/seed.py

test:
	uv run pytest

evals:
	uv run python evals/run.py --suite all

report:
	uv run python evals/report.py results/latest

verify-audit:
	uv run python service/verify_audit.py

tamper-demo:
	bash scripts/tamper_demo.sh

push-agent:
	uv run python agent/push.py

push-tests:
	uv run python evals/push_tests.py

tunnel:        ## show the public URL and check it resolves
	bash scripts/tunnel.sh

smoke:
	bash scripts/smoke.sh

psql:
	$(COMPOSE) exec postgres psql -U dg_admin -d dg

logs:
	$(COMPOSE) logs -f --tail=100 service n8n caddy ngrok
