"""Provider factory.

``get_provider()`` is the only place that decides which backend to construct,
so the rest of the application stays backend-agnostic (terv.md 3. fejezet).
The default is Ollama; cloud backends are optional and never mandatory
(terv.md 19. fejezet).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .base import LLMError, LLMProvider
from .ollama import OllamaProvider

PROVIDER_OLLAMA = "ollama"
PROVIDER_OPENAI = "openai-compatible"

_OPENAI_ALIASES = {"openai", "openai-compatible", "openai_compatible"}


class OpenAICompatibleProvider(LLMProvider):
    """Placeholder for an OpenAI-compatible HTTP backend.

    TODO: implement ``POST {base}/v1/chat/completions`` with bearer auth and
    JSON-mode output, then wire it into :func:`get_provider` via
    ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` (terv.md 19. fejezet). Cloud LLMs
    must stay optional.
    """

    name = PROVIDER_OPENAI

    def generate(self, prompt: str, **kwargs: Any) -> str:
        raise NotImplementedError("OpenAICompatibleProvider.generate is not implemented yet (TODO)")

    def structured(
        self,
        prompt: str,
        schema: type[BaseModel] | dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        raise NotImplementedError(
            "OpenAICompatibleProvider.structured is not implemented yet (TODO)"
        )


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
