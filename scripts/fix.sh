#!/usr/bin/env bash
# PrintForge auto-fix: applies every safe fix ruff/djlint can make.
set -u
cd "$(dirname "$0")/.." || exit 1

BACKEND=backend
FAILED=0

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is not on PATH" >&2
    exit 127
fi

run() {
    local name="$1" cmd="$2"
    printf '\n\033[1m==> %s\033[0m\n' "$name"
    bash -c "$cmd" || FAILED=1
}

run "ruff check --fix"  "cd $BACKEND && uv run ruff check --fix ."
run "ruff format"       "cd $BACKEND && uv run ruff format ."
run "djlint --reformat" "cd $BACKEND && uv run djlint ../frontend/templates --reformat"

exit "$FAILED"
