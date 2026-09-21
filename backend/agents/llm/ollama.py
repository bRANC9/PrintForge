"""Ollama HTTP backend for :class:`~agents.llm.base.LLMProvider`.

Uses only the standard library (``urllib.request``) so the project gains no
extra dependency (terv.md 3. / 18. fejezet). ``generate()`` calls
``POST {base}/api/generate`` and ``structured()`` calls ``POST {base}/api/chat``
with Ollama's structured-output ``format`` field.

``base_url``/``model`` are resolved from the runtime settings service
(``configuration.services.get_setting``: DB override -> Django settings ->
``os.environ`` -> default). Values are resolved **at construction time** —
never cached at import time — and can be refreshed with :meth:`reload`, so an
admin setting change takes effect on the next provider instance without a
process restart. Explicit constructor arguments always win; a ``get_setting``
seam can be injected for tests without touching the database.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from .base import LLMError, LLMProvider

DEFAULT_TIMEOUT_SEC = 120.0
DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen3-coder:30b"

#: Settings-resolver seam: ``(name) -> effective value``.
SettingGetter = Callable[[str], Any]

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
        get_setting: SettingGetter | None = None,
    ) -> None:
        self._get_setting: SettingGetter = get_setting or _service_setting
        self.timeout = float(timeout)
        self.system_prompt = system_prompt
        self.base_url = DEFAULT_BASE_URL
        self.model = DEFAULT_MODEL
        self.reload(base_url=base_url, model=model)

    def reload(self, *, base_url: str | None = None, model: str | None = None) -> None:
        """Re-resolve ``base_url``/``model`` from the settings service.

        Called by ``__init__``; call it again to pick up a runtime settings
        change (a provider instance is short-lived per call, so this is usually
        not needed). Explicit arguments win over the settings service, so
        ``reload(model="x")`` refreshes only the base URL.
        """
        resolved_base = base_url if base_url is not None else self._get_setting("ollama_base_url")
        resolved_model = model if model is not None else self._get_setting("ollama_model")
        self.base_url = str(_or_default(resolved_base, DEFAULT_BASE_URL)).rstrip("/")
        self.model = str(_or_default(resolved_model, DEFAULT_MODEL))

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


def _service_setting(name: str) -> Any:
    """Resolve a setting through the runtime settings service.

    ``configuration.services.get_setting`` (owned by ``api-dev``) is imported
    lazily so this module imports without Django configured, and so a runtime
    override is picked up on the next provider construction rather than being
    frozen at import time. Resolution order is DB override -> Django settings
    -> ``os.environ`` -> default, all handled by that service.
    """
    from configuration.services import get_setting

    return get_setting(name)


def _or_default(value: Any, default: str) -> Any:
    """Treat ``None``/blank as "not configured" and use *default*."""
    if value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    return value


def _message_content(result: dict[str, Any]) -> str | None:
    message = result.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None
