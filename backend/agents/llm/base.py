"""Backend-agnostic LLM interface.

The application must never be Ollama-specific (terv.md 3. fejezet). Every
agent (Planner, Research, CAD, Validator) talks to :class:`LLMProvider`; the
concrete HTTP backends live in sibling modules and are selected by
:mod:`agents.llm.factory`.

There is intentionally **no** shell execution and **no** ``eval`` anywhere in
this layer (terv.md 20. fejezet).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class LLMError(RuntimeError):
    """Raised when an LLM backend cannot produce a usable result.

    This is the single error type callers need to handle: transport failures,
    malformed responses and schema-validation failures are all translated into
    ``LLMError`` with a human-readable message.
    """


class LLMProvider(ABC):
    """Common interface for every LLM backend.

    Implementations must never execute shell commands or ``eval``. ``kwargs``
    are backend-specific knobs (e.g. ``temperature``, ``options``, ``system``)
    and should be ignored when unsupported.
    """

    #: Stable, human-readable backend identifier, e.g. ``"ollama"``.
    name: str = "base"

    @abstractmethod
    def generate(self, prompt: str, **kwargs: Any) -> str:
        """Return free-form text for *prompt*.

        Raises:
            LLMError: on transport failure, timeout or an empty response.
        """

    @abstractmethod
    def structured(
        self,
        prompt: str,
        schema: type[BaseModel] | dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Return a JSON object matching *schema*.

        *schema* is either a Pydantic v2 model class or a raw JSON Schema
        ``dict``. When a model class is given the parsed payload is validated
        against it before being returned as a plain ``dict``.

        Raises:
            LLMError: on transport failure, malformed JSON or a payload that
                does not match the requested schema.
        """

    @staticmethod
    def schema_to_json_schema(schema: type[BaseModel] | dict[str, Any]) -> dict[str, Any]:
        """Normalise *schema* to a JSON Schema ``dict``.

        Pydantic model classes are converted with ``model_json_schema()``;
        ``dict`` schemas pass through unchanged. Anything else raises
        :class:`TypeError` (programming error, not an :class:`LLMError`).
        """
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            return schema.model_json_schema()
        if isinstance(schema, dict):
            return schema
        raise TypeError(f"schema must be a Pydantic model class or a dict, got {type(schema)!r}")

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"
