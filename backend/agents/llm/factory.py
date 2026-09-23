"""Provider factory.

``get_provider()`` is the only place that decides which backend to construct,
so the rest of the application stays backend-agnostic (terv.md 3. fejezet).
The default is Ollama; cloud backends are optional and never mandatory
(terv.md 19. fejezet).
"""

from __future__ import annotations

from typing import Any

from .base import LLMError, LLMProvider
from .ollama import OllamaProvider
from .openai import OpenAICompatibleProvider

PROVIDER_OLLAMA = "ollama"
PROVIDER_OPENAI = "openai-compatible"

_OPENAI_ALIASES = {"openai", "openai-compatible", "openai_compatible"}


def get_provider(name: str | None = None, **kwargs: Any) -> LLMProvider:
    """Return the configured :class:`LLMProvider`.

    Args:
        name: Optional explicit backend name. Falls back to the
            ``LLM_PROVIDER`` Django setting and finally to ``"ollama"``.
        **kwargs: Forwarded to the provider constructor (e.g. ``base_url``,
            ``model``, ``timeout``).

    Raises:
        LLMError: if *name* is not a known backend.
    """
    from django.conf import settings

    selected = (name or getattr(settings, "LLM_PROVIDER", PROVIDER_OLLAMA)).strip().lower()
    if selected == PROVIDER_OLLAMA:
        return OllamaProvider(**kwargs)
    if selected in _OPENAI_ALIASES:
        return OpenAICompatibleProvider(**kwargs)
    raise LLMError(f"Unknown LLM provider: {selected!r}")
