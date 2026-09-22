"""Runtime-editable application settings (terv.md 19. fejezet).

The :class:`~configuration.models.AppSettings` singleton stores *overrides*.
Every effective value is resolved with this order:

1. the DB override (when the field is non-empty / non-``None``);
2. ``django.conf.settings.<UPPER>`` when that attribute exists;
3. ``os.environ[<UPPER>]``;
4. the hardcoded default.

``get_setting`` is cheap enough to call on every request: the singleton row is
cached briefly (30 s) and the cache is invalidated by :func:`update_settings`.

Consumers (LLM provider, embeddings, CAD, slicer, storage) must read values
through this module so a runtime override takes effect without a restart.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from django.conf import settings as django_settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from .models import AppSettings, OllamaPull, OllamaPullStatus
from .tasks import pull_ollama_model_task

logger = logging.getLogger(__name__)

__all__ = [
    "SETTING_NAMES",
    "OllamaError",
    "delete_ollama_model",
    "effective_settings",
    "get_setting",
    "get_settings",
    "invalidate_settings_cache",
    "list_ollama_models",
    "list_pulls",
    "ollama_version",
    "pull_ollama_model",
    "pull_status",
    "show_ollama_model",
    "test_ollama",
    "update_settings",
]

#: name -> (settings/env attribute, hardcoded default). The default's type also
#: drives coercion (``bool`` for ``rag_enabled``, ``int`` for ``*_sec``).
_SETTING_SPECS: dict[str, tuple[str, Any]] = {
    "ollama_base_url": ("OLLAMA_BASE_URL", "http://localhost:11434"),
    "ollama_model": ("OLLAMA_MODEL", "qwen3-coder:30b"),
    "embedding_model": ("EMBEDDING_MODEL", "bge-m3"),
    "rag_enabled": ("RAG_ENABLED", False),
    "openscad_mode": ("OPENSCAD_MODE", "local"),
    "openscad_timeout_sec": ("OPENSCAD_TIMEOUT_SEC", 60),
    "openscad_memory_limit": ("OPENSCAD_MEMORY_LIMIT", "1g"),
    "openscad_cpu_limit": ("OPENSCAD_CPU_LIMIT", "1.0"),
    "slicer_mode": ("SLICER_MODE", "local"),
    "slicer_timeout_sec": ("SLICER_TIMEOUT_SEC", 300),
    "storage_backend": ("STORAGE_BACKEND", "local"),
}

#: Names accepted by :func:`get_setting` / :func:`update_settings`.
SETTING_NAMES = tuple(_SETTING_SPECS)

#: Short-lived cache of the singleton row (invalidated on update).
_CACHE_KEY = "configuration:app_settings"
_CACHE_TTL = 30

#: Timeout for the Ollama reachability probe.
_OLLAMA_TEST_TIMEOUT = 3

_MISSING = object()


def get_settings() -> AppSettings:
    """Return the singleton :class:`AppSettings`, short-cached."""
    obj = cache.get(_CACHE_KEY)
    if obj is None:
        obj = AppSettings.load()
        cache.set(_CACHE_KEY, obj, _CACHE_TTL)
    return obj


def invalidate_settings_cache() -> None:
    """Drop the cached singleton (called by :func:`update_settings`)."""
    cache.delete(_CACHE_KEY)


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _coerce(name: str, value: Any) -> Any:
    """Coerce ``value`` to the type implied by the setting's default."""
    default = _SETTING_SPECS[name][1]
    if isinstance(default, bool):
        return _to_bool(value)
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc
    return str(value)


def _store_value(name: str, value: Any) -> Any:
    """Value to persist for a field: ``None``/``""`` means "fall back".

    Clearing is type-consistent: nullable bool/int fields store ``None``,
    non-null CharFields store ``""``. Both are treated as "no override".
    """
    if value is None or value == "":
        default = _SETTING_SPECS[name][1]
        # Nullable fields (bool/int) use None; non-null CharFields use "".
        return None if isinstance(default, (bool, int)) else ""
    return _coerce(name, value)


def _validate_value(name: str, value: Any) -> Any:
    """Validate a non-empty stored value against its model field.

    Enforces the field's ``choices`` and validators (max length, min value,
    ...) and returns the normalised value. Raises :class:`ValueError` naming the
    offending setting.

    ``URLField`` values are normalised but **not** URL-validated: Django's
    ``URLValidator`` rejects single-label hosts such as Docker service names
    (``http://ollama:11434``), which are legitimate here (terv.md 18.3).
    """
    field = AppSettings._meta.get_field(name)
    try:
        value = field.to_python(value)
        field.validate(value, None)
        if not isinstance(field, models.URLField):
            field.run_validators(value)
    except ValidationError as exc:
        messages = "; ".join(exc.messages)
        raise ValueError(f"{name}: {messages}") from exc
    return value


def _db_override(obj: AppSettings, name: str) -> Any:
    """Return the DB override, or :data:`_MISSING` when there is none."""
    value = getattr(obj, name)
    if value is None:
        return _MISSING
    if isinstance(value, str) and value == "":
        return _MISSING
    return value


def _resolve(obj: AppSettings, name: str) -> tuple[Any, str]:
    """Resolve ``name`` to ``(value, source)`` for ``obj``."""
    override = _db_override(obj, name)
    if override is not _MISSING:
        return _coerce(name, override), "db"

    upper, default = _SETTING_SPECS[name]
    if hasattr(django_settings, upper):
        value = getattr(django_settings, upper)
        if value is not None:
            return _coerce(name, value), "env"

    env_value = os.environ.get(upper)
    if env_value is not None:
        return _coerce(name, env_value), "env"

    return default, "default"


def get_setting(name: str) -> Any:
    """Return the effective value of ``name`` (DB -> settings -> env -> default)."""
    if name not in _SETTING_SPECS:
        raise ValueError(f"Unknown setting: {name}")
    value, _source = _resolve(get_settings(), name)
    return value


@transaction.atomic
def update_settings(*, user=None, **fields: Any) -> AppSettings:
    """Validate, persist and cache-invalidate setting overrides.

    Only names in :data:`SETTING_NAMES` are accepted. Every value is coerced to
    the setting's type and then validated against its model field (``choices``
    and validators), so an out-of-range/unknown choice is rejected before it can
    poison the runtime settings. Invalid values raise :class:`ValueError` naming
    the offending setting.

    Empty/``None`` values clear the override (fall back) for every type, and are
    never validated.
    """
    unknown = set(fields) - set(_SETTING_SPECS)
    if unknown:
        raise ValueError(f"Unknown setting(s): {', '.join(sorted(unknown))}")

    obj = get_settings()
    for name, value in fields.items():
        stored = _store_value(name, value)
        if stored is not None and stored != "":
            stored = _validate_value(name, stored)
        setattr(obj, name, stored)
    if user is not None:
        obj.updated_by = user
    obj.save()
    invalidate_settings_cache()
    return obj


def effective_settings() -> dict:
    """Return ``{"effective": {...}, "overrides": {...}, "sources": {...}}``.

    * ``effective`` -- the resolved value for every setting;
    * ``overrides`` -- the raw DB override, or ``None`` when falling back;
    * ``sources`` -- ``"db"`` | ``"env"`` | ``"default"`` per setting.
    """
    obj = get_settings()
    effective: dict[str, Any] = {}
    overrides: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for name in SETTING_NAMES:
        value, source = _resolve(obj, name)
        effective[name] = value
        sources[name] = source
        override = _db_override(obj, name)
        overrides[name] = None if override is _MISSING else _coerce(name, override)
    return {"effective": effective, "overrides": overrides, "sources": sources}


def test_ollama() -> dict:
    """Probe ``{ollama_base_url}/api/tags``; never raises.

    Returns ``{"ok": bool, "detail": str}``.
    """
    try:
        base_url = str(get_setting("ollama_base_url") or "").rstrip("/")
    except Exception as exc:  # noqa: BLE001 - never raise to the caller
        logger.exception("test_ollama could not resolve the base URL")
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    if not base_url:
        return {"ok": False, "detail": "No Ollama base URL configured."}

    url = f"{base_url}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=_OLLAMA_TEST_TIMEOUT) as response:
            status = getattr(response, "status", None) or response.getcode()
            response.read(64)
    except Exception as exc:  # noqa: BLE001 - never raise to the caller
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    if status == 200:
        return {"ok": True, "detail": f"Ollama is reachable at {base_url}."}
    return {"ok": False, "detail": f"Ollama returned HTTP {status}."}


# ---------------------------------------------------------------------------
# Ollama model management (list / show / pull / delete)
# ---------------------------------------------------------------------------

#: Timeout for short Ollama management calls (list/show/delete).
_OLLAMA_API_TIMEOUT = 10


class OllamaError(RuntimeError):
    """Ollama could not fulfil a model-management request."""


def _ollama_base_url() -> str:
    """Return the configured Ollama base URL without a trailing slash."""
    return str(get_setting("ollama_base_url") or "").rstrip("/")


def _ollama_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    timeout: int = _OLLAMA_API_TIMEOUT,
) -> dict:
    """Call ``url`` and decode the JSON response, raising :class:`OllamaError`."""
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise OllamaError(f"Ollama returned HTTP {exc.code}: {body[:200]}") from exc
    except Exception as exc:  # noqa: BLE001 - normalise transport errors
        raise OllamaError(f"{type(exc).__name__}: {exc}") from exc

    if not raw:
        return {}
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise OllamaError("Ollama returned an invalid JSON response.") from exc
    return decoded if isinstance(decoded, dict) else {"data": decoded}


def _human_size(num_bytes: Any) -> str | None:
    """Format a byte count as e.g. ``"4.7 GB"`` (``None`` when unknown)."""
    if num_bytes is None:
        return None
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return None
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def ollama_version() -> dict:
    """Return ``{"version": "..."}`` or ``{"error": "..."}``; never raises."""
    try:
        base_url = _ollama_base_url()
    except Exception as exc:  # noqa: BLE001 - never raise to the caller
        logger.exception("ollama_version could not resolve the base URL")
        return {"error": f"{type(exc).__name__}: {exc}"}
    if not base_url:
        return {"error": "No Ollama base URL configured."}
    try:
        data = _ollama_json(f"{base_url}/api/version")
    except OllamaError as exc:
        return {"error": str(exc)}
    version = data.get("version")
    if version is None:
        return {"error": "Unexpected response from Ollama /api/version."}
    return {"version": str(version)}


def list_ollama_models() -> list[dict]:
    """Return the locally available Ollama models (raises :class:`OllamaError`)."""
    base_url = _ollama_base_url()
    if not base_url:
        raise OllamaError("No Ollama base URL configured.")
    data = _ollama_json(f"{base_url}/api/tags")
    models = data.get("models") or []
    result: list[dict] = []
    for item in models:
        details = item.get("details") or {}
        size = item.get("size")
        result.append(
            {
                "name": item.get("name") or item.get("model") or "",
                "size": size,
                "size_human": _human_size(size),
                "modified_at": item.get("modified_at"),
                "digest": item.get("digest"),
                "family": details.get("family"),
                "parameter_size": details.get("parameter_size"),
                "quantization": details.get("quantization_level"),
            }
        )
    return result


def show_ollama_model(name: str) -> dict:
    """Return Ollama's ``/api/show`` payload for ``name``."""
    base_url = _ollama_base_url()
    if not base_url:
        raise OllamaError("No Ollama base URL configured.")
    if not (name or "").strip():
        raise OllamaError("A model name is required.")
    return _ollama_json(f"{base_url}/api/show", method="POST", payload={"name": name})


def delete_ollama_model(*, name: str) -> None:
    """Delete ``name`` from Ollama; raise :class:`OllamaError` on failure."""
    base_url = _ollama_base_url()
    if not base_url:
        raise OllamaError("No Ollama base URL configured.")
    if not (name or "").strip():
        raise OllamaError("A model name is required.")
    _ollama_json(f"{base_url}/api/delete", method="DELETE", payload={"name": name})


def pull_ollama_model(*, name: str, user=None) -> OllamaPull:
    """Create a ``PENDING`` :class:`OllamaPull` and enqueue the Celery task.

    If the broker is unavailable the row is marked ``FAILED`` (with the error)
    and returned, so the UI can render the failure instead of a 500.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("A model name is required.")

    pull = OllamaPull.objects.create(
        name=cleaned,
        status=OllamaPullStatus.PENDING,
        created_by=user,
    )
    try:
        pull_ollama_model_task.delay(pull.pk)
    except Exception as exc:  # noqa: BLE001 - broker can fail in many ways
        pull.status = OllamaPullStatus.FAILED
        pull.error = f"Could not enqueue the pull: {type(exc).__name__}: {exc}"
        pull.completed_at = timezone.now()
        pull.save(update_fields=["status", "error", "completed_at"])
    return pull


def list_pulls(limit: int = 20):
    """Return the most recent pulls, newest first."""
    return OllamaPull.objects.select_related("created_by").order_by("-created_at")[:limit]


def pull_status(pull: OllamaPull) -> dict:
    """Serialise an :class:`OllamaPull` for the API/UI."""
    return {
        "id": pull.pk,
        "name": pull.name,
        "status": pull.status,
        "progress_percent": pull.progress_percent,
        "detail": pull.detail,
        "completed_bytes": pull.completed_bytes,
        "total_bytes": pull.total_bytes,
        "error": pull.error,
        "created_at": pull.created_at.isoformat() if pull.created_at else None,
        "started_at": pull.started_at.isoformat() if pull.started_at else None,
        "completed_at": pull.completed_at.isoformat() if pull.completed_at else None,
    }
