"""Backend-agnostic LLM interface.

The application must never be Ollama-specific (terv.md 3. fejezet). Every
agent (Planner, Research, CAD, Validator) talks to :class:`LLMProvider`; the
concrete HTTP backends live in sibling modules and are selected by
:mod:`agents.llm.factory`.

Vision support (terv.md 27. fejezet) is **optional and queried, never
hardcoded**: callers ask :meth:`LLMProvider.supports_vision` before attaching a
reference image, and a provider that cannot see simply reports ``False``.
There is intentionally **no** shell execution and **no** ``eval`` anywhere in
this layer (terv.md 20. fejezet).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

#: A single image as raw bytes. ``bytearray``/``memoryview`` are accepted too
#: and normalised by :func:`normalise_images`.
ImageBytes = bytes


def normalise_images(images: Any) -> list[bytes]:
    """Normalise the *images* argument to a ``list[bytes]``.

    Accepted inputs:

    * ``None`` -> ``[]`` (no images; the default behaviour).
    * a single ``bytes``/``bytearray``/``memoryview`` -> a one-element list.
    * any iterable of ``bytes``/``bytearray``/``memoryview`` -> a list of
      immutable ``bytes`` objects.

    Raises:
        TypeError: for an unsupported type (programming error, not an
            :class:`LLMError`) — e.g. a ``str``, an ``int`` or a list
            containing non-buffer elements. This mirrors
            :meth:`LLMProvider.schema_to_json_schema`, which also signals
            caller mistakes with :class:`TypeError`.
        ValueError: when a supplied image is empty, which can never be a
            usable reference image.
    """
    if images is None:
        return []
    if isinstance(images, (bytes, bytearray, memoryview)):
        images = [images]
    elif isinstance(images, str) or not isinstance(images, Iterable):
        raise TypeError(
            "images must be bytes/bytearray/memoryview or an iterable of those, "
            f"got {type(images)!r}"
        )

    normalised: list[bytes] = []
    for image in images:
        if isinstance(image, memoryview):
            image = image.tobytes()
        elif isinstance(image, bytearray):
            image = bytes(image)
        elif not isinstance(image, bytes):
            raise TypeError(f"each image must be bytes/bytearray/memoryview, got {type(image)!r}")
        if not image:
            raise ValueError("images must not contain empty byte strings")
        normalised.append(image)
    return normalised


class LLMError(RuntimeError):
    """Raised when an LLM backend cannot produce a usable result.

    This is the single error type callers need to handle: transport failures,
    malformed responses, schema-validation failures and requests to use images
    on a non-vision model are all translated into ``LLMError`` with a
    human-readable message.
    """


class LLMProvider(ABC):
    """Common interface for every LLM backend.

    Implementations must never execute shell commands or ``eval``. ``kwargs``
    are backend-specific knobs (e.g. ``temperature``, ``options``, ``system``)
    and should be ignored when unsupported.

    Vision contract (terv.md 27. fejezet): :meth:`supports_vision` defaults to
    ``False``; concrete backends override it. When a caller passes ``images``
    to :meth:`generate`/:meth:`structured` on a provider that does not support
    vision, implementations **raise** :class:`LLMError` (they do not silently
    drop the images). Callers that need the terv.md fallback behaviour should
    catch :class:`LLMError`, warn and retry with ``images=None``.
    """

    #: Stable, human-readable backend identifier, e.g. ``"ollama"``.
    name: str = "base"

    def supports_vision(self) -> bool:
        """Return ``True`` if this provider can consume reference images.

        The base implementation returns ``False``. Backends that may have
        vision-capable models (e.g. Ollama) override this and inspect the
        model's own capability metadata rather than guessing from its name
        (terv.md 27.1).
        """
        return False

    @abstractmethod
    def generate(
        self,
        prompt: str,
        *,
        images: list[bytes] | None = None,
        **kwargs: Any,
    ) -> str:
        """Return free-form text for *prompt*.

        Args:
            prompt: Non-empty instruction text.
            images: Optional reference images (raw bytes). Only pass these
                after :meth:`supports_vision` returned ``True``; a provider
                that cannot see raises :class:`LLMError` instead of ignoring
                them.

        Raises:
            LLMError: on transport failure, timeout, an empty response, or
                images passed to a non-vision model.
        """

    @abstractmethod
    def structured(
        self,
        prompt: str,
        schema: type[BaseModel] | dict[str, Any],
        *,
        images: list[bytes] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Return a JSON object matching *schema*.

        *schema* is either a Pydantic v2 model class or a raw JSON Schema
        ``dict``. When a model class is given the parsed payload is validated
        against it before being returned as a plain ``dict``.

        Args:
            images: Optional reference images (raw bytes), same contract as
                :meth:`generate`.

        Raises:
            LLMError: on transport failure, malformed JSON, a payload that
                does not match the requested schema, or images passed to a
                non-vision model.
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
