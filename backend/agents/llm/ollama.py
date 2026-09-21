"""Ollama HTTP backend for :class:`~agents.llm.base.LLMProvider`.

Uses only the standard library (``urllib.request``) so the project gains no
extra dependency (terv.md 3. / 18. fejezet). ``generate()`` calls
``POST {base}/api/generate`` and ``structured()`` calls ``POST {base}/api/chat``
with Ollama's structured-output ``format`` field.

Configuration is read from Django settings (``OLLAMA_BASE_URL``,
``OLLAMA_MODEL``) but every value can be overridden per instance, which keeps
the provider usable from Celery workers and tests.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from pydantic import BaseModel, ValidationError

from .base import LLMError, LLMProvider

DEFAULT_TIMEOUT_SEC = 120.0
DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen3-coder:30b"

STRUCTURED_SYSTEM_PROMPT = (
    "You are a CAD specification assistant. Answer with a single JSON object "
    "and nothing else. Do not add explanations, markdown fences or comments."
)


class OllamaProvider(LLMProvider):
    """LLM backend speaking Ollama's native HTTP API."""

    name = "ollama"

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        *,
        system_prompt: str = STRUCTURED_SYSTEM_PROMPT,
    ) -> None:
        self.base_url = (base_url or _setting("OLLAMA_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.model = model or _setting("OLLAMA_MODEL", DEFAULT_MODEL)
        self.timeout = float(timeout)
        self.system_prompt = system_prompt

    # -- public API ---------------------------------------------------------

    def generate(self, prompt: str, **kwargs: Any) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise LLMError("generate() requires a non-empty prompt")

        payload: dict[str, Any] = {
            "model": kwargs.pop("model", self.model),
            "prompt": prompt,
            "stream": False,
        }
        system = kwargs.pop("system", None)
        if system:
            payload["system"] = system
        options = kwargs.pop("options", None)
        if options:
            payload["options"] = options
        # Forward any remaining backend-specific knobs (e.g. ``temperature``,
        # ``keep_alive``, ``think``) untouched.
        payload.update(kwargs)

        result = self._post("/api/generate", payload)
        text = result.get("response")
        if not isinstance(text, str) or not text.strip():
            raise LLMError("Ollama returned an empty generate response")
        return text

    def structured(
        self,
        prompt: str,
        schema: type[BaseModel] | dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise LLMError("structured() requires a non-empty prompt")

        json_schema = self.schema_to_json_schema(schema)
        system = kwargs.pop("system", self.system_prompt)
        payload: dict[str, Any] = {
            "model": kwargs.pop("model", self.model),
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{system}\n\nReturn only JSON conforming to this JSON Schema:\n"
                        f"{json.dumps(json_schema, ensure_ascii=False)}"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "format": json_schema,
        }
        options = kwargs.pop("options", None)
        if options:
            payload["options"] = options
        payload.update(kwargs)

        result = self._post("/api/chat", payload)
        content = _message_content(result)
        if content is None:
            raise LLMError("Ollama returned no message content in the structured response")

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"Ollama structured response is not valid JSON: {content[:200]!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise LLMError("Ollama structured response must be a JSON object")

        if isinstance(schema, type) and issubclass(schema, BaseModel):
            try:
                return schema.model_validate(parsed).model_dump()
            except ValidationError as exc:
                raise LLMError(f"Ollama response does not match {schema.__name__}: {exc}") from exc
        return parsed

    # -- transport ----------------------------------------------------------

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST *payload* as JSON to *path* and decode the JSON response.

        All transport errors are converted to :class:`LLMError`.
        """
        url = f"{self.base_url}{path}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except TimeoutError as exc:
            raise LLMError(f"Ollama request timed out after {self.timeout}s: {url}") from exc
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise LLMError(f"Ollama HTTP {exc.code} on {url}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"Cannot reach Ollama at {url}: {exc.reason}") from exc
        except OSError as exc:
            raise LLMError(f"Ollama request failed on {url}: {exc}") from exc

        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Ollama returned invalid JSON on {url}: {body[:200]!r}") from exc
        if not isinstance(result, dict):
            raise LLMError(
                f"Ollama returned an unexpected payload on {url}: {type(result).__name__}"
            )
        return result


def _setting(name: str, default: str) -> str:
    """Read a value from Django settings without requiring them at import time."""
    from django.conf import settings

    return getattr(settings, name, default)


def _message_content(result: dict[str, Any]) -> str | None:
    message = result.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None
