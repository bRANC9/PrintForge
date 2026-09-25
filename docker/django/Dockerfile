FROM python:3.13-slim AS base

# Commit the image was built from, surfaced by /api/v1/health/ and the page
# footer. The release workflow passes it as a build arg; a local build without it
# stays "dev". Declared *inside* the stage: ENV is only valid after a FROM.
ARG APP_GIT_SHA=dev
ENV APP_GIT_SHA=${APP_GIT_SHA}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    UV_PROJECT_ENVIRONMENT=/app/backend/.venv

# Pinned to the version used in CI (.github/workflows/ci.yml). Bump both together.
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /bin/

# Docker CLI (client only) so the CAD/slicer workers can launch ephemeral
# sandbox containers through the mounted Docker socket (Docker-out-of-Docker).
# The binary is a static Go build from the official CLI image; the Docker engine
# is NOT installed (the host daemon is used via /var/run/docker.sock).
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker

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

# Fallback only: the compose services run docker/django/entrypoint.sh, which
# builds the gunicorn command from the GUNICORN_* env vars. Keep these values in
# sync with the entrypoint defaults.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", \
     "--workers", "2", "--threads", "4", "--worker-class", "gthread", \
     "--timeout", "60", "--graceful-timeout", "30", "--keep-alive", "5", \
     "--max-requests", "1000", "--max-requests-jitter", "100", "--log-level", "info"]
