"""OpenAI-compatible backend for :class:`~agents.llm.base.LLMProvider`.

Talks to the official ``openai`` Python SDK, so it works with OpenAI itself and
with any gateway that speaks the same ``/v1/chat/completions`` protocol
(OpenRouter, vLLM, LM Studio, Ollama's OpenAI shim, ...). Cloud LLMs stay
optional (terv.md 19. fejezet): the provider is only constructed when the
``llm_provider`` setting selects it.

Settings are resolved at construction time through
``configuration.services.get_setting`` (DB override -> Django settings ->
``os.environ`` -> default), never cached at import time, so an admin change takes
effect on the next provider instance. Explicit constructor arguments always win,
and a ``get_setting`` seam can be injected for tests without touching Django.

Vision (terv.md 27. fejezet) is config-driven (:meth:`supports_vision` returns
the ``vision`` flag, default ``False``) because it depends on the selected model,
not on the endpoint. Images are sent as ``image_url`` data URIs.

The API key is a credential: it is read lazily, never logged and never included
in an error message. Every SDK error is translated to :class:`LLMError`; there is
no shell execution and no ``eval`` (terv.md 20. fejezet).
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from .base import LLMError, LLMProvider, normalise_images

DEFAULT_TIMEOUT_SEC = 120.0
#: The SDK's own default endpoint; an empty ``openai_base_url`` means "use it".
DEFAULT_BASE_URL = ""
DEFAULT_MODEL = "gpt-4o-mini"

#: Settings-resolver seam: ``(name) -> effective value``.
SettingGetter = Callable[[str], Any]

STRUCTURED_SYSTEM_PROMPT = (
    "You are a CAD specification assistant. Answer with a single JSON object "
    "and nothing else. Do not add explanations, markdown fences or comments."
)

#: Extra kwargs accepted by ``__init__`` that are *not* settings; everything else
#: is treated as a settings name so a future ``openai_model`` key needs no code
#: change here.
_NON_SETTING_KWARGS = frozenset(
    {
        "timeout",
        "system_prompt",
        "get_setting",
        "client",
        "vision",
        "api_key",
        "base_url",
        "model",
    }
)


class OpenAICompatibleProvider(LLMProvider):
    """LLM backend speaking the OpenAI ``chat.completions`` protocol."""

    name = "openai-compatible"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        *,
        vision: bool = False,
        system_prompt: str = STRUCTURED_SYSTEM_PROMPT,
        get_setting: SettingGetter | None = None,
        client: Any | None = None,
        **settings: Any,
    ) -> None:
        self._get_setting: SettingGetter = get_setting or _service_setting
        self.timeout = float(timeout)
        self.system_prompt = system_prompt
        self._vision = bool(vision)
        # Provider-specific ``openai_*`` settings (e.g. an explicit model name)
        # resolved through the same settings service and overridable per call.
        self._extra_settings = dict(settings)
        self._explicit_client = client
        self._client: Any | None = None
        self.base_url = DEFAULT_BASE_URL
        self.api_key = ""
        self.model = DEFAULT_MODEL
        self.reload(base_url=base_url, api_key=api_key, model=model)

    def reload(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        """Re-resolve ``base_url``/``api_key``/``model`` from the settings service.

        Called by ``__init__``; call it again to pick up a runtime settings
        change. Explicit arguments win over the settings service, so
        ``reload(model="x")`` refreshes only the endpoint.
        """
        resolved_base = base_url if base_url is not None else self._get_setting("openai_base_url")
        resolved_key = api_key if api_key is not None else self._get_setting("openai_api_key")
        resolved_model = model if model is not None else self._resolve_model_setting("openai_model")
        self.base_url = str(_or_default(resolved_base, DEFAULT_BASE_URL)).rstrip("/")
        self.api_key = str(_or_default(resolved_key, ""))
        self.model = str(_or_default(resolved_model, DEFAULT_MODEL))
        # Drop a client built for the previous endpoint/key.
        self._client = None

    # -- public API ---------------------------------------------------------

    def supports_vision(self) -> bool:
        """Return the configured vision flag (default ``False``).

        Vision depends on the selected model rather than the endpoint, so it is
        an explicit opt-in (terv.md 27.1): ``OpenAICompatibleProvider(vision=True)``.
        """
        return self._vision

    def generate(
        self,
        prompt: str,
        *,
        images: list[bytes] | None = None,
        **kwargs: Any,
    ) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise LLMError("generate() requires a non-empty prompt")
        image_bytes = normalise_images(images)
        self._ensure_vision(image_bytes)

        messages: list[dict[str, Any]] = []
        system = kwargs.pop("system", None)
        if system:
            messages.append({"role": "system", "content": str(system)})
        messages.append({"role": "user", "content": self._user_content(prompt, image_bytes)})

        response = self._create_completion(messages, kwargs.pop("model", None), kwargs)
        text = _message_content(response)
        if not text or not text.strip():
            raise LLMError("OpenAI-compatible endpoint returned an empty generate response")
        return text

    def structured(
        self,
        prompt: str,
        schema: type[BaseModel] | dict[str, Any],
        *,
        images: list[bytes] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise LLMError("structured() requires a non-empty prompt")
        image_bytes = normalise_images(images)
        self._ensure_vision(image_bytes)

        json_schema = self.schema_to_json_schema(schema)
        system = kwargs.pop("system", self.system_prompt)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": str(system)},
            {"role": "user", "content": self._user_content(prompt, image_bytes)},
        ]
        response_format = _build_response_format(json_schema, kwargs.pop("name", None))

        response = self._create_completion(
            messages,
            kwargs.pop("model", None),
            kwargs,
            response_format=response_format,
        )
        content = _message_content(response)
        if content is None:
            raise LLMError("OpenAI-compatible endpoint returned no message content")

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"OpenAI-compatible structured response is not valid JSON: {content[:200]!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise LLMError("OpenAI-compatible structured response must be a JSON object")

        if isinstance(schema, type) and issubclass(schema, BaseModel):
            try:
                return schema.model_validate(parsed).model_dump()
            except ValidationError as exc:
                raise LLMError(
                    f"OpenAI-compatible response does not match {schema.__name__}: {exc}"
                ) from exc
        return parsed

    # -- internals ----------------------------------------------------------

    def _ensure_vision(self, image_bytes: list[bytes]) -> None:
        """Fail fast when images are passed to a provider without vision enabled.

        terv.md 27.1 describes a graceful fallback; raising :class:`LLMError`
        keeps that decision on the caller (catch, warn, retry with
        ``images=None``) instead of silently discarding the user's image.
        """
        if image_bytes and not self.supports_vision():
            raise LLMError(
                "OpenAI-compatible provider is not configured for vision; pass "
                "vision=True with a vision-capable model, or omit images"
            )

    def _user_content(self, prompt: str, image_bytes: list[bytes]) -> Any:
        """Build the user message content, adding images as data URIs.

        Returns a plain string when there are no images (maximally compatible
        with text-only gateways) and the multimodal content list otherwise.
        """
        if not image_bytes:
            return prompt
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in image_bytes:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _data_uri(image)},
                }
            )
        return content

    def _create_completion(
        self,
        messages: list[dict[str, Any]],
        model: str | None,
        kwargs: dict[str, Any],
        *,
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        """Call ``chat.completions.create`` and map every SDK error to LLMError."""
        request: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
        }
        if response_format is not None:
            request["response_format"] = response_format
        # Forward any remaining backend-specific knobs (temperature, max_tokens,
        # top_p, ...) untouched.
        request.update(kwargs)

        client = self._get_client()
        try:
            return client.chat.completions.create(**request)
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 - translate any SDK failure
            raise LLMError(f"OpenAI-compatible request failed: {_safe_message(exc)}") from exc

    def _get_client(self) -> Any:
        """Return the lazily constructed SDK client (never logs the key)."""
        if self._explicit_client is not None:
            return self._explicit_client
        if self._client is None:
            # Lazy import so this module loads (and tests run) without the SDK
            # installed / without a configured endpoint.
            from openai import OpenAI

            self._client = OpenAI(
                base_url=self.base_url or None,
                api_key=self.api_key or None,
                timeout=self.timeout,
            )
        return self._client

    def _resolve_model_setting(self, name: str) -> Any:
        """Resolve an arbitrary settings name, falling back to Django/env.

        ``configuration.services.get_setting`` only knows a fixed set of names,
        so on the calling thread it can reject ``openai_model`` before it is
        registered. The injected seam can override the resolution for tests.
        """
        try:
            value = self._get_setting(name)
        except Exception:  # noqa: BLE001 - unknown name -> fall through
            return _django_or_env(name)
        if value is not None:
            return value
        return _django_or_env(name)


# ---------------------------------------------------------------------------
# Settings resolution helpers
# ---------------------------------------------------------------------------


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


def _django_or_env(name: str) -> Any:
    """Resolve ``name`` from Django settings then ``os.environ`` (UPPER_CASE)."""
    upper = name.upper()
    try:
        from django.conf import settings

        if hasattr(settings, upper):
            value = getattr(settings, upper)
            if value is not None:
                return value
    except Exception:  # noqa: BLE001 - Django not configured
        pass
    return os.environ.get(upper)


def _or_default(value: Any, default: str) -> Any:
    """Treat ``None``/blank as "not configured" and use *default*."""
    if value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    return value


# ---------------------------------------------------------------------------
# Payload/response helpers
# ---------------------------------------------------------------------------


def _build_response_format(json_schema: dict[str, Any], name: str | None) -> dict[str, Any]:
    """Build the SDK ``response_format`` dict from a JSON Schema.

    Mirrors Ollama's structured output: the model is constrained to a single
    JSON object matching the schema. ``strict`` is left off because several
    compatible gateways validate it more aggressively than OpenAI does.
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name or str(json_schema.get("title") or "response"),
            "schema": json_schema,
        },
    }


def _data_uri(image: bytes) -> str:
    """Return a ``data:`` URI for *image* (PNG type is the common case)."""
    return f"data:image/png;base64,{base64.b64encode(image).decode('ascii')}"


def _safe_message(exc: Exception) -> str:
    """Render *exc* for an error message without ever leaking credentials.

    The SDK's own message can embed the base URL but not the key; ``str(exc)``
    is used unchanged, and the exception type is always included so a bare
    message-less error is still diagnosable.
    """
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text[:500]}" if text else type(exc).__name__


def _message_content(response: Any) -> str | None:
    """Extract ``choices[0].message.content`` from an SDK response, if any."""
    choices = getattr(response, "choices", None)
    if not choices:
        return None
    message = getattr(choices[0], "message", None)
    if message is None and isinstance(choices[0], dict):
        message = choices[0].get("message")
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    return content if isinstance(content, str) else None
