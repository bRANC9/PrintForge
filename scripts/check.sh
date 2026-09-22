#!/usr/bin/env bash
# PrintForge quality gate: runs every read-only check and prints a summary.
# Non-zero exit when any gate fails, so it is CI-safe.
set -u
cd "$(dirname "$0")/.." || exit 1

BACKEND=backend
FAILED=0
RESULTS=()

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is not on PATH" >&2
    exit 127
fi

run() {
    local name="$1" cmd="$2"
    printf '\n\033[1m==> %s\033[0m\n' "$name"
    if bash -c "$cmd"; then
        RESULTS+=("PASS  $name")
    else
        RESULTS+=("FAIL  $name")
        FAILED=1
    fi
}

run "ruff check"   "cd $BACKEND && uv run ruff check ."
run "ruff format"  "cd $BACKEND && uv run ruff format --check ."
run "djlint"       "cd $BACKEND && uv run djlint ../frontend/templates --check"
run "django check" "cd $BACKEND && uv run python manage.py check"
# Prefer parallel (pytest-xdist: one test DB per worker); fall back to serial
# when xdist is not installed. Override: PYTEST_ARGS="-n 4 --dist load".
if (cd "$BACKEND" && uv run python -c "import xdist" >/dev/null 2>&1); then
    DEFAULT_PYTEST_ARGS="-n auto --dist loadscope -p no:cacheprovider"
else
    DEFAULT_PYTEST_ARGS=""
fi
run "pytest"       "cd $BACKEND && uv run pytest ${PYTEST_ARGS:-$DEFAULT_PYTEST_ARGS}"

printf '\n\033[1m===== SUMMARY =====\033[0m\n'
for row in "${RESULTS[@]}"; do
    echo "  $row"
done
if [ "$FAILED" -eq 0 ]; then
    echo "  ALL GATES PASSED"
else
    echo "  SOME GATES FAILED"
fi
exit "$FAILED"
