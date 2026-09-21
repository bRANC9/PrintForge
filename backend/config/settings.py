"""Django settings for PrintForge.

Values come from environment variables (see .env.example). The defaults are
safe for local development only.
"""

from pathlib import Path

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

# Parse the database URL up front. Some apps depend on PostgreSQL-only features
# (the `embeddings` app uses pgvector) and must only be registered when the
# default database is PostgreSQL. See the Applications section below.
_DATABASE_CONFIG = env.db_url("DATABASE_URL", default="sqlite:///" + str(BASE_DIR / "db.sqlite3"))
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
    "designs",
    "agents",
    "files",
    "printers",
    "slicers",
    "notifications",
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

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

MEDIA_URL = "media/"
MEDIA_ROOT = env("MEDIA_ROOT", default=str(BASE_DIR / "media"))

STORAGE_BACKEND = env("STORAGE_BACKEND", default="local")

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

# Embedding / RAG
EMBEDDING_MODEL = env("EMBEDDING_MODEL", default="bge-m3")
EMBEDDING_DIM = env("EMBEDDING_DIM")
RAG_ENABLED = env("RAG_ENABLED")

# OpenSCAD sandbox defaults (see terv.md 20. fejezet)
OPENSCAD_TIMEOUT_SEC = env.int("OPENSCAD_TIMEOUT_SEC", default=60)
OPENSCAD_MEMORY_LIMIT = env("OPENSCAD_MEMORY_LIMIT", default="1g")
OPENSCAD_CPU_LIMIT = env("OPENSCAD_CPU_LIMIT", default="1.0")

# Agent workflow (see terv.md 6-7. fejezet)
AGENT_MAX_ATTEMPTS = env.int("AGENT_MAX_ATTEMPTS", default=3)
