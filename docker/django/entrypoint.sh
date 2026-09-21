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

case "$1" in
    worker)
        exec celery -A config worker --loglevel=info
        ;;
    web|"")
        exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3
        ;;
    *)
        exec "$@"
        ;;
esac
