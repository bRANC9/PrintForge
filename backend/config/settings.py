"""Django settings for PrintForge.

Values come from environment variables (see .env.example). The defaults are
safe for local development only.
"""

from pathlib import Path
from urllib.parse import quote

import environ

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    RAG_ENABLED=(bool, False),
    EMBEDDING_DIM=(int, 1024),
)
environ.Env.read_env(REPO_ROOT / ".env")


def _fallback_database_url() -> str:
    """Build the default DB URL when ``DATABASE_URL`` is not set.

    PostgreSQL is derived from the standard ``POSTGRES_*`` variables only when
    ``POSTGRES_PASSWORD`` is explicitly provided, so the password is written
    once (e.g. in the TrueNAS compose file) and reused by ``web``/``worker``.
    The password is URL-encoded so special characters do not break the URL.
    With no ``POSTGRES_PASSWORD`` (local dev / tests) this keeps the sqlite
    fallback unchanged.
    """
    password = env.str("POSTGRES_PASSWORD", default="")
    if not password:
        return "sqlite:///" + str(BASE_DIR / "db.sqlite3")

    user = quote(env.str("POSTGRES_USER", default="printforge"), safe="")
    host = env.str("POSTGRES_HOST", default="db")
    port = env.int("POSTGRES_PORT", default=5432)
    name = env.str("POSTGRES_DB", default="printforge")
    return f"postgres://{user}:{quote(password, safe='')}@{host}:{port}/{name}"


# Parse the database URL up front. ``DATABASE_URL`` wins when set; otherwise a
# Postgres URL is derived from ``POSTGRES_*`` (or sqlite for local dev). Some
# apps depend on PostgreSQL-only features (the `embeddings` app uses pgvector)
# and must only be registered when the default database is PostgreSQL. See the
# Applications section below.
_DATABASE_CONFIG = env.db_url("DATABASE_URL", default=_fallback_database_url())
DB_IS_POSTGRES = _DATABASE_CONFIG["ENGINE"] == "django.db.backends.postgresql"

SECRET_KEY = env("DJANGO_SECRET_KEY", default="insecure-dev-key-change-me")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

# ---------------------------------------------------------------------------
# Production / reverse proxy — all off by default for local HTTP dev
# ---------------------------------------------------------------------------
# Enable these via env when deploying behind HTTPS (e.g. TrueNAS + a reverse
# proxy). With defaults (all off) nothing changes for local http://<ip>:8080.

SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=False)
SESSION_COOKIE_SECURE = env.bool("DJANGO_SESSION_COOKIE_SECURE", default=False)
CSRF_COOKIE_SECURE = env.bool("DJANGO_CSRF_COOKIE_SECURE", default=False)

SECURE_HSTS_SECONDS = env.int("DJANGO_SECURE_HSTS_SECONDS", default=0)
if SECURE_HSTS_SECONDS > 0:
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

if env.bool("DJANGO_SECURE_PROXY_SSL_HEADER", default=False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
]

THIRD_PARTY_APPS = [
    "rest_framework",
]

LOCAL_APPS = [
    "accounts",
    "workspaces",
    "projects",
    "skills",
    "designs",
    "agents",
    "files",
    "printers",
    "slicers",
    "notifications",
    "configuration",
    "api",
    "mcp",
]

# pgvector's VectorField/VectorExtension cannot run on sqlite, so the app (and
# its migrations) is only registered on PostgreSQL. This keeps the sqlite-based
# test suite and local development working without the extension.
if DB_IS_POSTGRES:
    LOCAL_APPS.append("embeddings")

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves static files from gunicorn (no separate web server needed).
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [REPO_ROOT / "frontend" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Database (PostgreSQL + pgvector in Docker; sqlite fallback for local tests)
# ---------------------------------------------------------------------------

DATABASES = {
    "default": _DATABASE_CONFIG,
}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "home"

# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "hu"
TIME_ZONE = "Europe/Budapest"
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static & media
# ---------------------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [REPO_ROOT / "frontend" / "static"]

# Compressed + manifest: gzip on the wire and a content hash in every filename
# (`app.<hash>.js`). The hash is what makes cache-busting work: a new build emits
# new URLs, so browsers never serve a stale asset after a deploy. Do not switch
# back to CompressedStaticFilesStorage -- that one keeps stable filenames and
# requires a hard refresh on every release.
# The forgiving subclass keeps that behaviour but falls back to the original
# filename when the manifest is absent (tests, fresh checkout, CI), instead of
# raising "Missing staticfiles manifest entry" on every template render.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "config.storage.ForgivingCompressedManifestStaticFilesStorage"},
}

MEDIA_URL = "media/"
MEDIA_ROOT = env("MEDIA_ROOT", default=str(BASE_DIR / "media"))

# Storage backend selection plus S3/MinIO credentials. ``files.services`` reads
# the AWS_* keys straight from these Django settings (the runtime singleton only
# knows ``storage_backend``), so they are deliberately not part of
# ``configuration.services``. They are only used when STORAGE_BACKEND == "s3";
# the empty defaults keep local development and the sqlite test suite working
# without any credentials.
STORAGE_BACKEND = env("STORAGE_BACKEND", default="local")
AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID", default="")
AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY", default="")
AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME", default="")
AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL", default="")
AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", default="")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticatedOrReadOnly",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
}

# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------

CELERY_BROKER_URL = env("REDIS_URL", default="redis://redis:6379/0")
CELERY_RESULT_BACKEND = env("REDIS_URL", default="redis://redis:6379/0")
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE

# ---------------------------------------------------------------------------
# PrintForge settings
# ---------------------------------------------------------------------------

# LLM / Ollama
OLLAMA_BASE_URL = env("OLLAMA_BASE_URL", default="http://localhost:11434")
OLLAMA_MODEL = env("OLLAMA_MODEL", default="qwen3-coder:30b")
# Optional dedicated vision model for the agent self-check review (empty = use
# the main OLLAMA_MODEL). Runtime override: `ollama_vision_model`.
OLLAMA_VISION_MODEL = env("OLLAMA_VISION_MODEL", default="")

# LLM provider selection (terv.md 19. fejezet). "ollama" is the default and
# needs nothing else; set it to "openai" to use any OpenAI-compatible endpoint
# (hosted OpenAI or a local gateway), which is when OPENAI_BASE_URL /
# OPENAI_API_KEY / OPENAI_MODEL are read. Cloud LLMs are always optional.
# Runtime overrides: `llm_provider`, `openai_base_url`, `openai_api_key`,
# `openai_model`.
LLM_PROVIDER = env("LLM_PROVIDER", default="ollama")
OPENAI_BASE_URL = env("OPENAI_BASE_URL", default="")
OPENAI_API_KEY = env("OPENAI_API_KEY", default="")
OPENAI_MODEL = env("OPENAI_MODEL", default="gpt-4o-mini")

# Embedding / RAG
EMBEDDING_MODEL = env("EMBEDDING_MODEL", default="bge-m3")
EMBEDDING_DIM = env("EMBEDDING_DIM")
RAG_ENABLED = env("RAG_ENABLED")

# Web search (Research agent). Empty SEARCH_BACKEND disables web search; set it
# to "searxng" to use the optional self-hosted instance from
# docker-compose.search.yml. SEARXNG_BASE_URL is only read when SEARCH_BACKEND
# is set, so the default is harmless for existing deployments. Runtime
# overrides: `search_backend`, `searxng_base_url`.
SEARCH_BACKEND = env("SEARCH_BACKEND", default="")
SEARXNG_BASE_URL = env("SEARXNG_BASE_URL", default="")

# OpenSCAD sandbox defaults (see terv.md 20. fejezet)
OPENSCAD_TIMEOUT_SEC = env.int("OPENSCAD_TIMEOUT_SEC", default=60)
OPENSCAD_MEMORY_LIMIT = env("OPENSCAD_MEMORY_LIMIT", default="1g")
OPENSCAD_CPU_LIMIT = env("OPENSCAD_CPU_LIMIT", default="1.0")

# Agent workflow (see terv.md 6-7. fejezet)
AGENT_MAX_ATTEMPTS = env.int("AGENT_MAX_ATTEMPTS", default=3)

# MCP transport identity (terv.md 25. fejezet). The numeric Django user id that
# every MCP tool acts as; None (the default) disables identity-dependent tools.
# ``mcp.server`` reads this straight from the Django setting / environment, so
# it is intentionally not part of ``configuration.services``.
MCP_SERVICE_USER_ID = env("MCP_SERVICE_USER_ID", default=None)
