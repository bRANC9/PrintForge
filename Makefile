# PrintForge developer tasks.
#
# One command for the whole gate (auto-fix + lint + tests + summary):
#
#     make            # == make all: fix what can be fixed, then run every gate
#
# Run it from the repo root (inside WSL):
#
#     wsl.exe -e bash -lc "cd '/home/branc/VS projects/PrintForge' && make"
#
SHELL := /bin/bash
.DEFAULT_GOAL := all

.PHONY: all help fix check test lint

all: ## Auto-fix (ruff, djlint) then run every gate and print a summary
	@echo "===================== FIX ====================="
	@bash scripts/fix.sh || true
	@echo
	@echo "==================== CHECK ===================="
	@bash scripts/check.sh

fix: ## Apply safe auto-fixes: ruff check --fix, ruff format, djlint --reformat
	@bash scripts/fix.sh

check: ## Run every read-only gate (ruff, djlint, django check, pytest) + summary
	@bash scripts/check.sh

test: ## Run the backend test suite (pytest)
	@cd backend && uv run pytest

lint: ## Run read-only lint/format checks (ruff + djlint)
	@cd backend && uv run ruff check . && uv run ruff format --check . && uv run djlint ../frontend/templates --check

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-6s\033[0m %s\n", $$1, $$2}'
