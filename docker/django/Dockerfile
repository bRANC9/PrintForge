FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    UV_PROJECT_ENVIRONMENT=/app/backend/.venv

# Pinned to the version used in CI (.github/workflows/ci.yml). Bump both together.
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /bin/

WORKDIR /app

# Dependencies first (better layer caching).
COPY backend/pyproject.toml backend/uv.lock ./backend/
RUN cd backend && uv sync --frozen --no-dev --no-install-project

# Application code.
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY docker/ ./docker/

RUN chmod +x /app/docker/django/entrypoint.sh

ENV PATH="/app/backend/.venv/bin:$PATH"
WORKDIR /app/backend

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
