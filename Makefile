.DEFAULT_GOAL := help
SHELL := /bin/bash

PY        := .venv/bin/python
PIP       := .venv/bin/pip
UVICORN   := .venv/bin/uvicorn
PYTEST    := .venv/bin/pytest
REDIS_BIN := /opt/homebrew/opt/redis/bin
PG_BIN    := /opt/homebrew/opt/postgresql@17/bin
DB        := budget_controller

# Homebrew's redis 8.10 bottle ships a config that loads modules it does not
# contain, so `brew services start redis` aborts. We run the same binary
# against infra/redis.conf instead.

.PHONY: help setup infra-up infra-down db-create db-drop migrate seed dev mock demo test test-criteria reconcile clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Create venv and install dependencies
	@bash scripts/setup.sh

infra-up: ## Start Redis (project config) and PostgreSQL
	@mkdir -p .data/redis
	@$(REDIS_BIN)/redis-cli ping >/dev/null 2>&1 || $(REDIS_BIN)/redis-server infra/redis.conf --daemonize yes
	@brew services list | grep -q "postgresql@17 started" || brew services start postgresql@17
	@sleep 1 && $(REDIS_BIN)/redis-cli ping && echo "postgres: $$($(PG_BIN)/pg_isready)"

infra-down: ## Stop Redis (leaves PostgreSQL running)
	@$(REDIS_BIN)/redis-cli shutdown nosave 2>/dev/null || true
	@echo "redis stopped"

db-create: ## Create the PostgreSQL database
	@$(PG_BIN)/createdb $(DB) 2>/dev/null || echo "database $(DB) already exists"

db-drop: ## Drop the PostgreSQL database
	@$(PG_BIN)/dropdb --if-exists $(DB)

migrate: ## Apply Alembic migrations
	@.venv/bin/alembic upgrade head

seed: ## Seed 2 teams / 4 products / 12 agents and the model catalog
	@$(PY) -m scripts.seed

dev: ## Run the budget proxy on :8000
	@$(UVICORN) app.main:app --reload --port 8000

mock: ## Run the mock LLM provider on :9000
	@$(UVICORN) mock_llm.main:app --port 9000

demo: ## Start infra + mock + proxy and open the dashboard
	@bash scripts/demo.sh

test: ## Run the full test suite
	@$(PYTEST) -q

test-criteria: ## Run only the six success-criteria tests
	@$(PYTEST) -q tests/criteria -v

reconcile: ## Rebuild Redis counters from the PostgreSQL ledger
	@$(PY) -m scripts.reconcile

clean: ## Remove caches and local data
	@find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .pytest_cache .embedded
	@echo "cleaned"
