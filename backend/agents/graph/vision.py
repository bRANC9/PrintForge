"""Optional reference-image handling for the agent workflow (terv.md 27. fejezet).

The reference photo is **always optional**: when the state carries no image the
helpers below behave exactly like a plain ``provider.structured`` call, so
today's text-only pipeline is unchanged.

Vision is queried, never assumed. The chosen policy is:

1. If the state has no reference image -> call the provider text-only and
   report ``image_used=False``.
2. If the provider reports ``supports_vision() is False`` -> **do not** send
   the image (the provider contract guarantees an :class:`~agents.llm.LLMError`
   otherwise); call text-only and record a warning. The run continues.
3. If the provider claims vision support -> attach ``images=[...]``. If the
   call still raises :class:`~agents.llm.LLMError` (e.g. a vision-capable model
   that rejects the payload), retry **without** the image and record a warning.

In no case does a reference image fail the run, and in no case is it silently
dropped: every path returns whether the image was used and, when it was not, a
human-readable warning for the state/``AgentRun.state_json``.
"""

from __future__ import annotations

import logging
from typing import Any

from agents.llm import LLMError, LLMProvider, normalise_images

__all__ = [
    "VISION_FALLBACK_WARNING",
    "VISION_UNSUPPORTED_WARNING",
    "reference_images",
    "reference_prompt",
    "reference_prompt_for",
    "structured_with_reference_image",
]

logger = logging.getLogger(__name__)

#: Recorded when a provider that claims vision support rejects the image.
VISION_FALLBACK_WARNING = (
    "Reference image was rejected by the selected model; the run continued from "
    "the text prompt only."
)
#: Recorded when the provider reports no vision support at all.
VISION_UNSUPPORTED_WARNING = (
    "Reference image was not sent: the selected model does not support vision; "
    "the run continued from the text prompt only."
)


def reference_images(state: dict[str, Any]) -> list[bytes]:
    """Return the reference image(s) carried by the workflow *state*.

    The state keeps at most one image today, but the provider contract accepts a
    list, so this normalises to ``[]`` (absent) or ``[bytes]``.
    """
    image = state.get("reference_image")
    if not image:
        return []
    return normalise_images(image)


def reference_prompt(state: dict[str, Any], *, vision: bool = False) -> str:
    """Return the user prompt enriched with the reference note/image hint.

    With no reference data this is exactly ``state["prompt"]``, keeping the
    text-only path byte-for-byte identical to before terv.md 27. fejezet.

    Args:
        state: Running workflow state.
        vision: ``provider.supports_vision()``. When ``False`` the "photo is
            attached" hint is omitted, because the image will not be sent.
    """
    prompt = state.get("prompt", "")
    note = (state.get("reference_image_note") or "").strip()
    if note:
        prompt = f"{prompt}\n\nReference note: {note}"
    if vision and state.get("reference_image"):
        prompt = (
            f"{prompt}\n\nA reference photo is attached; use its shape, "
            "proportions, function and any visible text as guidance."
        )
    return prompt


def reference_prompt_for(provider: LLMProvider, state: dict[str, Any]) -> str:
    """Build the node prompt, querying vision only when an image is present.

    ``supports_vision()`` may be a network call for some backends (e.g. Ollama
    inspects ``/api/show``), so a text-only run must not trigger it. The
    ``and`` short-circuits: no image -> no capability query.
    """
    has_image = bool(state.get("reference_image"))
    return reference_prompt(state, vision=has_image and provider.supports_vision())


def structured_with_reference_image(
    provider: LLMProvider,
    prompt: str,
    schema: type | dict[str, Any],
    *,
    system: str,
    images: list[bytes] | None = None,
) -> tuple[dict[str, Any], bool, str | None]:
    """Call ``provider.structured`` with an optional reference image.

    Args:
        provider: The injected LLM provider (never a concrete backend).
        prompt: User/instruction prompt.
        schema: Pydantic model class or raw JSON Schema dict.
        system: System prompt for the call.
        images: Raw reference image bytes (``None``/empty for text-only).

    Returns:
        ``(payload, image_used, warning)``. ``image_used`` is ``True`` only when
        the image was actually accepted by the provider; ``warning`` explains
        why it was not used when it was present but skipped/rejected.

    Raises:
        LLMError: only for a text-only failure. An image-specific failure is
            always retried without the image.
    """
    normalised = normalise_images(images)

    if not normalised:
        return provider.structured(prompt, schema, system=system), False, None

    if not provider.supports_vision():
        logger.warning(
            "LLM provider %s does not support vision; sending the reference image is skipped",
            provider.name,
        )
        return provider.structured(prompt, schema, system=system), False, VISION_UNSUPPORTED_WARNING

    try:
        payload = provider.structured(prompt, schema, system=system, images=normalised)
    except LLMError as exc:
        logger.warning(
            "Reference image rejected by LLM provider %s, retrying text-only: %s",
            provider.name,
            exc,
        )
        return provider.structured(prompt, schema, system=system), False, VISION_FALLBACK_WARNING

    return payload, True, None
