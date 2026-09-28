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
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any, NamedTuple

from django.conf import settings as django_settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from . import model_catalog
from .models import AppSettings, OllamaPull, OllamaPullStatus
from .tasks import pull_ollama_model_task

logger = logging.getLogger(__name__)

__all__ = [
    "PULL_STALE_SECONDS",
    "SETTING_NAMES",
    "OllamaError",
    "RemoteCatalogError",
    "cancel_pull",
    "delete_ollama_model",
    "effective_settings",
    "get_setting",
    "get_settings",
    "invalidate_settings_cache",
    "is_secret_setting",
    "list_ollama_library_models",
    "list_ollama_models",
    "list_pulls",
    "model_recommendations",
    "ollama_version",
    "pull_ollama_model",
    "pull_is_stale",
    "pull_status",
    "retry_pull",
    "search_huggingface_models",
    "show_ollama_model",
    "test_ollama",
    "update_settings",
]


class SettingSpec(NamedTuple):
    """Registry entry for a runtime setting.

    ``attr`` is the Django settings / ``os.environ`` attribute name, ``default``
    is the hardcoded fallback and ``secret`` flags credentials.

    The entry remains tuple-shaped (``spec[0]`` / ``spec[1]``), so consumers
    written against the previous ``(attr, default)`` registry keep working.
    """

    attr: str
    default: Any
    secret: bool = False


#: name -> registry entry. The ``default``'s type also drives coercion
#: (``bool`` for ``rag_enabled``, ``int`` for ``*_sec``, ``float`` for
#: ``mesh_default_scale_mm``).
_SETTING_SPECS: dict[str, SettingSpec] = {
    "ollama_base_url": SettingSpec("OLLAMA_BASE_URL", "http://localhost:11434"),
    "ollama_model": SettingSpec("OLLAMA_MODEL", "qwen3-coder:30b"),
    "ollama_vision_model": SettingSpec("OLLAMA_VISION_MODEL", ""),
    "ollama_timeout": SettingSpec("OLLAMA_TIMEOUT", 120),
    "ollama_context_length": SettingSpec("OLLAMA_CONTEXT_LENGTH", 0),
    "llm_provider": SettingSpec("LLM_PROVIDER", "ollama"),
    "openai_base_url": SettingSpec("OPENAI_BASE_URL", ""),
    "openai_api_key": SettingSpec("OPENAI_API_KEY", "", secret=True),
    "openai_model": SettingSpec("OPENAI_MODEL", "gpt-4o-mini"),
    "embedding_model": SettingSpec("EMBEDDING_MODEL", "bge-m3"),
    "rag_enabled": SettingSpec("RAG_ENABLED", False),
    "search_backend": SettingSpec("SEARCH_BACKEND", ""),
    "searxng_base_url": SettingSpec("SEARXNG_BASE_URL", ""),
    "openscad_mode": SettingSpec("OPENSCAD_MODE", "local"),
    "openscad_timeout_sec": SettingSpec("OPENSCAD_TIMEOUT_SEC", 60),
    "openscad_memory_limit": SettingSpec("OPENSCAD_MEMORY_LIMIT", "1g"),
    "openscad_cpu_limit": SettingSpec("OPENSCAD_CPU_LIMIT", "1.0"),
    "mesh_backend": SettingSpec("MESH_BACKEND", ""),
    "mesh_repair_enabled": SettingSpec("MESH_REPAIR_ENABLED", True),
    "mesh_target_faces": SettingSpec("MESH_TARGET_FACES", 50000),
    "mesh_default_scale_mm": SettingSpec("MESH_DEFAULT_SCALE_MM", 120.0),
    "mesh_max_source_bytes": SettingSpec("MESH_MAX_SOURCE_BYTES", 64 * 1024 * 1024),
    "slicer_mode": SettingSpec("SLICER_MODE", "local"),
    "slicer_timeout_sec": SettingSpec("SLICER_TIMEOUT_SEC", 300),
    "storage_backend": SettingSpec("STORAGE_BACKEND", "local"),
    "mcp_service_user_id": SettingSpec("MCP_SERVICE_USER_ID", ""),
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
    default = _SETTING_SPECS[name].default
    if isinstance(default, bool):
        return _to_bool(value)
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a number") from exc
    return str(value)


def _store_value(name: str, value: Any) -> Any:
    """Value to persist for a field: ``None``/``""`` means "fall back".

    Clearing is type-consistent: nullable bool/int/float fields store ``None``,
    non-null CharFields store ``""``. Both are treated as "no override".
    """
    if value is None or value == "":
        default = _SETTING_SPECS[name].default
        # Nullable fields (bool/int/float) use None; non-null CharFields use "".
        return None if isinstance(default, (bool, int, float)) else ""
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

    upper, default = _SETTING_SPECS[name][:2]
    if hasattr(django_settings, upper):
        value = getattr(django_settings, upper)
        if value is not None:
            return _coerce(name, value), "env"

    env_value = os.environ.get(upper)
    if env_value is not None:
        return _coerce(name, env_value), "env"

    return default, "default"


def is_secret_setting(name: str) -> bool:
    """Whether the runtime setting ``name`` holds a credential.

    Backed by the first-class ``secret`` marker on :data:`_SETTING_SPECS`.
    Unknown names return ``False``; callers that need extra protection (e.g.
    the API masking layer) may add a name-based heuristic on top.
    """
    spec = _SETTING_SPECS.get(name)
    return bool(spec.secret) if spec is not None else False


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
                # Ollama >= 0.34 reports e.g. ["completion", "tools"] or
                # ["embedding"]; older versions omit it (empty list => show all).
                "capabilities": [str(cap) for cap in (item.get("capabilities") or [])],
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


#: A pull that has not progressed for this long is *stuck*: its worker is gone
#: (container restart, OOM kill) or it was never picked up from the queue. Such a
#: row is offered for retry instead of polling forever.
PULL_STALE_SECONDS = 900


def pull_is_stale(pull: OllamaPull) -> bool:
    """Whether an unfinished pull looks abandoned and should be retried."""
    if pull.status not in {OllamaPullStatus.PENDING, OllamaPullStatus.RUNNING}:
        return False
    reference = pull.started_at or pull.created_at
    if reference is None:
        return True
    return (timezone.now() - reference).total_seconds() > PULL_STALE_SECONDS


def cancel_pull(pull: OllamaPull) -> OllamaPull:
    """Mark a pull cancelled so the worker aborts and the UI stops tracking it.

    A pull that is already finished is left alone: cancelling it would rewrite
    history of a download that actually completed.
    """
    if pull.status not in {OllamaPullStatus.PENDING, OllamaPullStatus.RUNNING}:
        return pull
    pull.status = OllamaPullStatus.CANCELLED
    pull.error = ""
    pull.detail = "Megszakítva a felhasználó által"[:300]
    pull.completed_at = timezone.now()
    pull.save(update_fields=["status", "error", "detail", "completed_at"])
    return pull


def retry_pull(pull: OllamaPull) -> OllamaPull:
    """Re-enqueue a finished, failed, cancelled or stuck pull.

    The row is reset to ``PENDING`` and the Celery task is handed the same id, so
    the history keeps one entry per download attempt instead of piling up.
    """
    pull.status = OllamaPullStatus.PENDING
    pull.error = ""
    pull.detail = "Újraindítva"
    pull.progress_percent = 0
    pull.completed_bytes = 0
    pull.total_bytes = 0
    pull.started_at = None
    pull.completed_at = None
    pull.save(
        update_fields=[
            "status",
            "error",
            "detail",
            "progress_percent",
            "completed_bytes",
            "total_bytes",
            "started_at",
            "completed_at",
        ]
    )
    try:
        pull_ollama_model_task.delay(pull.pk)
    except Exception as exc:  # noqa: BLE001 - the broker can fail in many ways
        pull.status = OllamaPullStatus.FAILED
        pull.error = f"Nem sikerült újraindítani: {type(exc).__name__}: {exc}"[:1000]
        pull.completed_at = timezone.now()
        pull.save(update_fields=["status", "error", "completed_at"])
    return pull


def pull_status(pull: OllamaPull) -> dict[str, Any]:
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
        # A stuck row is actionable: the UI offers retry/cancel for it.
        "stale": pull_is_stale(pull),
        "created_at": pull.created_at.isoformat() if pull.created_at else None,
        "started_at": pull.started_at.isoformat() if pull.started_at else None,
        "completed_at": pull.completed_at.isoformat() if pull.completed_at else None,
    }


# ---------------------------------------------------------------------------
# Model advisor: VRAM recommendation + remote catalogs (ollama.com / HF)
# ---------------------------------------------------------------------------


class RemoteCatalogError(RuntimeError):
    """A remote model catalog (ollama.com / huggingface.co) could not be read."""


#: Timeout for the remote catalog HTTP calls.
_REMOTE_TIMEOUT = 8

#: ollama.com exposes an OpenAI-ish list of (mostly cloud) models at these URLs.
OLLAMA_LIBRARY_URL = "https://ollama.com/api/tags"
HUGGINGFACE_MODELS_URL = "https://huggingface.co/api/models"
HUGGINGFACE_MODEL_URL = "https://huggingface.co/api/models/{repo}"

#: A GGUF filename stem is split on ``-``/``_``; these two decide which token run
#: is the quantization label. The head starts it, the tail extends it.
#:
#: Repos put the quant anywhere in the name (``model-Q4_K_M.gguf``,
#: ``model-NVFP4-MTP.gguf``, ``UD-IQ3_XXS-....gguf``), so the old
#: "token just before .gguf" pattern missed NVFP4-only repos and the catalog
#: offered their F16 sibling instead -- a 27B model at full precision is over
#: 50 GB. Tokenising instead of one big regex also keeps filename noise out of
#: the label: ``UD-IQ3_XXS-model`` yields ``UD-IQ3_XXS``, not
#: ``UD-IQ3_XXS-M``.
_QUANT_HEAD = re.compile(
    r"^(?:NVFP4|MXFP4|FP4|FP16|BF16|F16|F32|FP32|EXL\d+|Q\d+|IQ\d+)$",
    re.IGNORECASE,
)
_QUANT_TAIL = re.compile(
    r"^(?:XXS|XS|XLL|XL|XV|KV|KM|KS|KL|K|M|S|L|BIT|MTP|\d+)$",
    re.IGNORECASE,
)
_UD_PREFIX = "ud"

#: Full-precision builds are not quantizations and must never be suggested
#: automatically.
_FULL_PRECISION_QUANTS = frozenset({"BF16", "F16", "FP16", "F32", "FP32"})

#: 1-2 bit quants are unusably degraded: still listed, never chosen.
_UNUSABLE_QUANT_PREFIXES = ("IQ1_", "IQ2_")

#: Preferred quantizations for a consumer GPU, best quality per size.
_QUANT_PREFERENCE = (
    "Q4_K_M",
    "Q4_K_S",
    "Q5_K_M",
    "Q5_K_S",
    "Q6_K",
    "Q8_0",
    "Q4_0",
    "Q3_K_M",
    "Q2_K",
)

#: A pull above this size does not fit a consumer GPU, so the size-based
#: fallback prefers the largest file below it.
_FITS_CONSUMER_GPU_BYTES = 24 * 10**9


def _quant_label(stem: str) -> str:
    """The quantization label inside a GGUF filename stem (``""`` when absent).

    Splits the stem on ``-``/``_``, finds the first :data:`_QUANT_HEAD` token and
    returns the original substring from there through the run of
    :data:`_QUANT_TAIL` tokens, so the returned label is exactly the suffix the
    repository used (that is the tag Ollama expects).
    """
    tokens = [
        (match.group(0), match.start(), match.end()) for match in re.finditer(r"[^_-]+", stem)
    ]
    for index, (text, _start, _end) in enumerate(tokens):
        if not _QUANT_HEAD.match(text):
            continue
        first = index
        if index > 0 and tokens[index - 1][0].lower() == _UD_PREFIX:
            first = index - 1  # keep the ``UD-`` prefix of UD-* quants
        last = index
        while last + 1 < len(tokens) and _QUANT_TAIL.match(tokens[last + 1][0]):
            last += 1
        # A sharded file appends ``-00001-of-00002``; that is a chunk index, not
        # part of the quant, so cut the label there.
        if last + 1 < len(tokens) and tokens[last + 1][0].lower() == "of":
            for back in range(last, index - 1, -1):
                if tokens[back][0].isdigit():
                    last = back - 1
                    break
        if last < first:  # pragma: no cover - defensive
            return ""
        return stem[tokens[first][1] : tokens[last][2]]
    return ""


def _normalise_quant(quant: str) -> str:
    """Upper-case a quant label with ``_`` separators, for comparisons."""
    return str(quant or "").upper().replace("-", "_")


def _is_usable_quant(quant: str) -> bool:
    """Whether *quant* is a real (non full-precision, non 1-2 bit) quantization."""
    normalised = _normalise_quant(quant)
    if not normalised or normalised in _FULL_PRECISION_QUANTS:
        return False
    return not normalised.startswith(_UNUSABLE_QUANT_PREFIXES)


def _remote_json(url: str, *, timeout: int = _REMOTE_TIMEOUT) -> Any:
    """GET ``url`` and decode JSON, raising :class:`RemoteCatalogError`."""
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "PrintForge/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except Exception as exc:  # noqa: BLE001 - normalise every transport error
        raise RemoteCatalogError(f"{type(exc).__name__}: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise RemoteCatalogError("The remote catalog returned invalid JSON.") from exc


def list_ollama_library_models(*, limit: int = 50) -> list[dict]:
    """Return the model list published by ollama.com (cloud/featured)."""
    data = _remote_json(OLLAMA_LIBRARY_URL)
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise RemoteCatalogError("Unexpected response from ollama.com/api/tags.")
    result: list[dict] = []
    for item in models[: max(int(limit), 1)]:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("model") or ""
        if not name:
            continue
        size = item.get("size")
        result.append(
            {
                "name": name,
                "pull_name": name,
                "size": size,
                "size_human": _human_size(size),
                "modified_at": item.get("modified_at"),
                "source": "ollama",
            }
        )
    return result


def _hf_quant_files(repo: str) -> list[dict[str, Any]]:
    """Every quantized GGUF file of *repo* with its real byte size.

    The detail request asks for ``blobs=true``: without it Hugging Face returns
    the file list with **no sizes**, so the catalog could not tell a 4 GB Q4_K_M
    from a 54 GB F16 and had to guess a quantization.
    """
    try:
        data = _remote_json(f"{HUGGINGFACE_MODEL_URL.format(repo=repo)}?blobs=true")
    except RemoteCatalogError:
        return []
    siblings = data.get("siblings") if isinstance(data, dict) else None
    if not isinstance(siblings, list):
        return []
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sibling in siblings:
        filename = str((sibling or {}).get("rfilename") or "")
        if not filename.lower().endswith(".gguf"):
            continue
        quant = _quant_label(filename[: -len(".gguf")])
        try:
            size = int((sibling or {}).get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        key = _normalise_quant(quant) or filename
        if key in seen:
            continue
        seen.add(key)
        files.append(
            {
                "quant": quant,
                "file": filename,
                "size": size,
                "size_human": _human_size(size),
            }
        )
    return files


def _pick_quant(files: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose the file to pull from the quantized candidates.

    Only real quantizations compete: a full-precision or 1-2 bit build is never
    returned, so a repo that ships nothing usable simply has no pull candidate
    instead of a 50 GB+ download. The fallback prefers the largest file that
    still fits a consumer GPU, then the smallest one available.
    """
    usable = [item for item in files if _is_usable_quant(item.get("quant", ""))]
    if not usable:
        return None
    for preferred in _QUANT_PREFERENCE:
        for item in usable:
            if _normalise_quant(item["quant"]) == preferred:
                return item
    by_size = sorted(usable, key=lambda item: int(item.get("size") or 0))
    fitting = [
        item for item in by_size if 0 < int(item.get("size") or 0) <= _FITS_CONSUMER_GPU_BYTES
    ]
    return (fitting or by_size)[-1]


def _hf_quants(repo: str) -> list[str]:
    """The usable quantization labels of *repo* (full precision excluded)."""
    return [item["quant"] for item in _hf_quant_files(repo) if _is_usable_quant(item["quant"])]


def search_huggingface_models(query: str = "", *, limit: int = 10) -> list[dict]:
    """Search Hugging Face for GGUF models runnable via ``hf.co/<repo>``.

    The quant list comes from a second request per repo (the search response
    does not embed the file list); those lookups run in a small thread pool.
    """
    params = {
        "filter": "gguf",
        "sort": "downloads",
        "direction": "-1",
        "limit": str(max(int(limit), 1)),
    }
    if query:
        params["search"] = query
    url = f"{HUGGINGFACE_MODELS_URL}?{urllib.parse.urlencode(params)}"
    data = _remote_json(url)
    if not isinstance(data, list):
        raise RemoteCatalogError("Unexpected response from huggingface.co/api/models.")

    result: list[dict] = []
    for item in data[: max(int(limit), 1)]:
        repo = (item or {}).get("id") or ""
        if not repo:
            continue
        result.append(
            {
                "name": repo,
                "downloads": item.get("downloads"),
                "likes": item.get("likes"),
                "quants": [],
                "source": "huggingface",
            }
        )

    if result:
        with ThreadPoolExecutor(max_workers=min(4, len(result))) as pool:
            for entry, files in zip(
                result,
                pool.map(lambda e: _hf_quant_files(e["name"]), result),
                strict=True,
            ):
                quantized = [item for item in files if item["quant"]]
                entry["quants"] = [
                    item["quant"] for item in quantized if _is_usable_quant(item["quant"])
                ]
                # Sizes next to every quant so the UI can show what a pull costs.
                entry["quant_sizes"] = {
                    item["quant"]: item["size_human"]
                    for item in quantized
                    if _is_usable_quant(item["quant"])
                }
                chosen = _pick_quant(files)
                if chosen is None:
                    # Only full-precision / 1-2 bit builds: offer no download at
                    # all rather than a 50 GB+ file.
                    entry["pull_name"] = ""
                    continue
                entry["pull_name"] = f"hf.co/{entry['name']}:{chosen['quant']}"
                entry["size"] = chosen["size"] or None
                entry["size_human"] = chosen["size_human"]
    return result


def model_recommendations(
    *,
    vram_gb: float,
    context: int = 8192,
    category: str = "",
    installed: list[dict] | None = None,
) -> dict:
    """Rank catalog + installed models against a VRAM budget.

    ``installed`` is normally read from the live Ollama instance; a failed
    lookup degrades to an empty list instead of breaking the advisor.
    """
    if vram_gb is None or float(vram_gb) <= 0:
        raise ValueError("vram_gb must be a positive number.")
    if installed is None:
        try:
            installed = list_ollama_models()
        except OllamaError:
            installed = []
    categories = [category] if category else None
    return {
        "vram_gb": float(vram_gb),
        "context": int(context),
        "category": category,
        "vram_tiers": list(model_catalog.VRAM_TIERS),
        "recommendations": model_catalog.recommend(
            float(vram_gb),
            context=int(context),
            categories=categories,
            installed=installed,
        ),
        "tiers": model_catalog.recommendation_tiers(
            context=int(context),
            categories=categories,
        ),
    }
