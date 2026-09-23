"""Provider factory.

``get_provider()`` is the only place that decides which backend to construct,
so the rest of the application stays backend-agnostic (terv.md 3. fejezet).
The default is Ollama; cloud backends are optional and never mandatory
(terv.md 19. fejezet).

The backend name is resolved at call time -- never cached at import time -- so a
runtime ``llm_provider`` override takes effect on the next provider instance
without a process restart. Resolution order:

1. an explicit ``name`` argument;
2. ``configuration.services.get_setting("llm_provider")`` -- the runtime
   settings service, which itself resolves DB override -> Django settings ->
   ``os.environ`` -> default;
3. ``django.conf.settings.LLM_PROVIDER`` when that service is unavailable;
4. the hardcoded ``"ollama"`` default.

The service is imported lazily and every lookup failure is swallowed, so the
factory keeps working when the database or Django settings are not reachable.
There is no shell execution and no ``eval`` anywhere in this layer
(terv.md 20. fejezet).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import LLMError, LLMProvider
from .ollama import OllamaProvider
from .openai import OpenAICompatibleProvider

PROVIDER_OLLAMA = "ollama"
PROVIDER_OPENAI = "openai-compatible"

_OPENAI_ALIASES = {"openai", "openai-compatible", "openai_compatible"}

#: Runtime setting name holding the selected backend.
_PROVIDER_SETTING = "llm_provider"

#: Settings-resolver seam: ``(name) -> effective value``.
SettingGetter = Callable[[str], Any]


def get_provider(
    name: str | None = None,
    *,
    get_setting: SettingGetter | None = None,
    **kwargs: Any,
) -> LLMProvider:
    """Return the configured :class:`LLMProvider`.

    Args:
        name: Optional explicit backend name. When blank/``None`` the runtime
            ``llm_provider`` setting decides (see the module docstring).
        get_setting: Optional settings-resolver seam, ``(name) -> value``. It is
            used for the ``llm_provider`` lookup and then forwarded to the
            provider so one injected seam controls the whole construction
            (tests only; production leaves it ``None``).
        **kwargs: Forwarded to the provider constructor (e.g. ``base_url``,
            ``model``, ``timeout``).

    Raises:
        LLMError: if the resolved name is not a known backend.
    """
    selected = _resolve_provider_name(name, get_setting).strip().lower()
    if selected == PROVIDER_OLLAMA:
        return OllamaProvider(get_setting=get_setting, **kwargs)
    if selected in _OPENAI_ALIASES:
        return OpenAICompatibleProvider(get_setting=get_setting, **kwargs)
    raise LLMError(f"Unknown LLM provider: {selected!r}")


def _resolve_provider_name(name: str | None, get_setting: SettingGetter | None) -> str:
    """Resolve the backend name without ever raising for a missing setting.

    Order: explicit *name* -> runtime settings service -> Django settings ->
    ``"ollama"``. A blank/``None`` value at any step falls through to the next,
    so a partially configured environment cannot break provider construction.
    """
    if name is not None and name.strip():
        return name

    value = _service_provider_setting(get_setting)
    if value is not None and str(value).strip():
        return str(value)

    fallback = _django_provider_setting()
    if fallback is not None and str(fallback).strip():
        return str(fallback)

    return PROVIDER_OLLAMA


def _service_provider_setting(get_setting: SettingGetter | None) -> Any:
    """Read ``llm_provider`` through the injected seam or the settings service."""
    try:
        resolver = get_setting or _service_setting
        return resolver(_PROVIDER_SETTING)
    except Exception:  # noqa: BLE001 - a missing service/DB must not break the factory
        return None


def _django_provider_setting() -> Any:
    """Read ``LLM_PROVIDER`` from Django settings, or ``None`` when unavailable."""
    try:
        from django.conf import settings

        return getattr(settings, "LLM_PROVIDER", None)
    except Exception:  # noqa: BLE001 - Django not configured
        return None


def _service_setting(name: str) -> Any:
    """Resolve a setting through the runtime settings service.

    ``configuration.services.get_setting`` is imported lazily so this module
    imports without Django configured, and so a runtime override is picked up on
    the next provider construction rather than being frozen at import time.
    Resolution order is DB override -> Django settings -> ``os.environ`` ->
    default, all handled by that service.
    """
    from configuration.services import get_setting

    return get_setting(name)
