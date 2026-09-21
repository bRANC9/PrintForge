#!/usr/bin/env sh
set -e

cd /app/backend

wait_for_db() {
    echo "Waiting for database..."
    python - <<'PY'
import os
import sys
import time

import psycopg

url = os.environ.get("DATABASE_URL", "")
if not url.startswith(("postgres://", "postgresql://")):
    sys.exit(0)

for attempt in range(60):
    try:
        with psycopg.connect(url):
            print("Database is up.")
            sys.exit(0)
    except Exception as exc:  # noqa: BLE001
        print(f"  not ready ({attempt + 1}/60): {exc}")
        time.sleep(2)
sys.exit(1)
PY
}

wait_for_db

python manage.py migrate --noinput

# Collect static files so WhiteNoise can serve them from gunicorn.
python manage.py collectstatic --noinput

case "$1" in
    worker)
        exec celery -A config worker --loglevel=info
        ;;
    web|"")
        # Gunicorn is configurable via env so operators can tune it without a
        # rebuild. The default worker class is gthread: unlike sync, it survives
        # the idle/keep-alive connections that make workers hit WORKER TIMEOUT.
        GUNICORN_WORKERS="${GUNICORN_WORKERS:-2}"
        GUNICORN_THREADS="${GUNICORN_THREADS:-4}"
        GUNICORN_WORKER_CLASS="${GUNICORN_WORKER_CLASS:-gthread}"
        GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-60}"
        GUNICORN_GRACEFUL_TIMEOUT="${GUNICORN_GRACEFUL_TIMEOUT:-30}"
        GUNICORN_KEEPALIVE="${GUNICORN_KEEPALIVE:-5}"
        GUNICORN_MAX_REQUESTS="${GUNICORN_MAX_REQUESTS:-1000}"
        GUNICORN_MAX_REQUESTS_JITTER="${GUNICORN_MAX_REQUESTS_JITTER:-100}"
        GUNICORN_ACCESS_LOG="${GUNICORN_ACCESS_LOG:-false}"
        GUNICORN_LOG_LEVEL="${GUNICORN_LOG_LEVEL:-info}"

        set -- gunicorn config.wsgi:application \
            --bind 0.0.0.0:8000 \
            --workers "$GUNICORN_WORKERS" \
            --threads "$GUNICORN_THREADS" \
            --worker-class "$GUNICORN_WORKER_CLASS" \
            --timeout "$GUNICORN_TIMEOUT" \
            --graceful-timeout "$GUNICORN_GRACEFUL_TIMEOUT" \
            --keep-alive "$GUNICORN_KEEPALIVE" \
            --max-requests "$GUNICORN_MAX_REQUESTS" \
            --max-requests-jitter "$GUNICORN_MAX_REQUESTS_JITTER" \
            --log-level "$GUNICORN_LOG_LEVEL"

        # Access logging is off by default; turn it on to diagnose timeouts.
        if [ "$GUNICORN_ACCESS_LOG" = "true" ] || [ "$GUNICORN_ACCESS_LOG" = "1" ]; then
            set -- "$@" --access-logfile -
        fi

        echo "Starting gunicorn: $*"
        exec "$@"
        ;;
    *)
        exec "$@"
        ;;
esac
