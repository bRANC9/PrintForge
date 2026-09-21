"""Backend-agnostic LLM layer.

Public entry points::

    from agents.llm import LLMError, LLMProvider, get_provider

    provider = get_provider()                     # Ollama by default
    text = provider.generate("Explain OpenSCAD")
    data = provider.structured(prompt, ModelSpecification)

Consumers should depend on ``LLMProvider`` / ``get_provider`` only, never on a
concrete backend (terv.md 3. fejezet).
"""

from .base import LLMError, LLMProvider
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
]
