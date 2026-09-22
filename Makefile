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

# Full-suite pytest flags. Prefer parallel (pytest-xdist gives each worker its
# own test DB); fall back to serial when xdist is not installed. Override any
# time:  make test PYTEST_ARGS=
PYTEST_HAS_XDIST := $(shell cd backend && uv run python -c "import xdist" >/dev/null 2>&1 && echo yes)
ifeq ($(PYTEST_HAS_XDIST),yes)
PYTEST_ARGS ?= -n auto --dist loadscope -p no:cacheprovider
else
PYTEST_ARGS ?=
endif
export PYTEST_ARGS

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

test: ## Run the backend test suite (pytest, parallel by default)
	@cd backend && uv run pytest $(PYTEST_ARGS)

lint: ## Run read-only lint/format checks (ruff + djlint)
	@cd backend && uv run ruff check . && uv run ruff format --check . && uv run djlint ../frontend/templates --check

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-6s\033[0m %s\n", $$1, $$2}'
