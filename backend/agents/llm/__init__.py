"""Backend-agnostic LLM layer.

Public entry points::

    from agents.llm import LLMError, LLMProvider, get_provider

    provider = get_provider()                     # Ollama by default
    text = provider.generate("Explain OpenSCAD")
    data = provider.structured(prompt, ModelSpecification)

Consumers should depend on ``LLMProvider`` / ``get_provider`` only, never on a
concrete backend (terv.md 3. fejezet). Reference images are optional and
guarded by ``LLMProvider.supports_vision`` (terv.md 27. fejezet)::

    if provider.supports_vision():
        data = provider.structured(prompt, ModelSpecification, images=[raw_png])
"""

from .base import LLMError, LLMProvider, normalise_images
from .factory import PROVIDER_OLLAMA, PROVIDER_OPENAI, OpenAICompatibleProvider, get_provider
from .ollama import OllamaProvider

__all__ = [
    "LLMError",
    "LLMProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "PROVIDER_OLLAMA",
    "PROVIDER_OPENAI",
    "get_provider",
    "normalise_images",
]
