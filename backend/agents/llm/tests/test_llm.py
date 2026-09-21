"""Provider contract, factory and Ollama transport tests (no network)."""

from __future__ import annotations

import io
import json
import sys
import types
import urllib.error
from typing import Any

import pytest
from pydantic import BaseModel

from agents.llm import (
    LLMError,
    LLMProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    get_provider,
)
from agents.llm.ollama import _service_setting
from agents.spec import ModelSpecification

VALID_SPEC: dict[str, Any] = ModelSpecification.example()

SERVICE_VALUES: dict[str, str] = {
    "ollama_base_url": "http://from-service:11434/",
    "ollama_model": "service-model",
}


def fake_settings(values: dict[str, Any] | None = None):
    """Return a ``get_setting`` seam backed by a plain dict (no DB, no Django)."""
    data = SERVICE_VALUES if values is None else values

    def get_setting(name: str) -> Any:
        return data.get(name)

    return get_setting


class FakeProvider(LLMProvider):
    """In-memory provider used to verify the interface contract."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def generate(self, prompt: str, **kwargs: Any) -> str:
        self.calls.append(("generate", prompt))
        return "fake answer"

    def structured(
        self,
        prompt: str,
        schema: type[BaseModel] | dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(("structured", schema))
        return {"fake": True}


# ---------------------------------------------------------------------------
# Interface & factory
# ---------------------------------------------------------------------------


def test_llm_provider_is_abstract():
    with pytest.raises(TypeError):
        LLMProvider()  # type: ignore[abstract]


def test_fake_provider_satisfies_the_interface():
    provider = FakeProvider()
    assert provider.generate("hello") == "fake answer"
    assert provider.structured("hello", ModelSpecification) == {"fake": True}
    assert provider.calls == [("generate", "hello"), ("structured", ModelSpecification)]


def test_get_provider_defaults_to_ollama():
    provider = get_provider(get_setting=fake_settings())
    assert isinstance(provider, OllamaProvider)
    assert provider.name == "ollama"
    assert provider.base_url == "http://from-service:11434"
    assert provider.model == "service-model"


def test_get_provider_forwards_overrides():
    provider = get_provider("ollama", base_url="http://example:11434/", model="tiny", timeout=5)
    assert provider.base_url == "http://example:11434"
    assert provider.model == "tiny"
    assert provider.timeout == 5.0


def test_get_provider_returns_openai_stub():
    provider = get_provider("openai-compatible")
    assert isinstance(provider, OpenAICompatibleProvider)
    with pytest.raises(NotImplementedError):
        provider.generate("hello")
    with pytest.raises(NotImplementedError):
        provider.structured("hello", ModelSpecification)


def test_get_provider_rejects_unknown_name():
    with pytest.raises(LLMError):
        get_provider("does-not-exist")


# ---------------------------------------------------------------------------
# Runtime settings resolution (injected seam, no Django settings, no DB)
# ---------------------------------------------------------------------------


def test_settings_are_resolved_from_injected_service():
    provider = OllamaProvider(get_setting=fake_settings())
    assert provider.base_url == "http://from-service:11434"
    assert provider.model == "service-model"


def test_explicit_values_win_over_settings_service():
    def exploding(name: str) -> Any:
        raise AssertionError(f"settings service must not be queried for {name}")

    provider = OllamaProvider(
        base_url="http://explicit:11434/",
        model="explicit-model",
        get_setting=exploding,
    )
    assert provider.base_url == "http://explicit:11434"
    assert provider.model == "explicit-model"


def test_blank_settings_fall_back_to_defaults():
    provider = OllamaProvider(
        get_setting=fake_settings({"ollama_base_url": "  ", "ollama_model": None})
    )
    assert provider.base_url == "http://localhost:11434"
    assert provider.model == "qwen3-coder:30b"


def test_reload_picks_up_runtime_settings_change():
    live = {"ollama_base_url": "http://old:11434", "ollama_model": "old-model"}
    provider = OllamaProvider(get_setting=lambda name: live[name])
    assert (provider.base_url, provider.model) == ("http://old:11434", "old-model")

    live.update(ollama_base_url="http://new:11434", ollama_model="new-model")
    provider.reload()
    assert (provider.base_url, provider.model) == ("http://new:11434", "new-model")


def test_reload_keeps_explicit_values():
    live = {"ollama_base_url": "http://svc:11434", "ollama_model": "svc-model"}
    provider = OllamaProvider(get_setting=lambda name: live[name])
    provider.reload(model="override")
    assert (provider.base_url, provider.model) == ("http://svc:11434", "override")


def test_default_resolver_uses_module_seam(monkeypatch):
    monkeypatch.setattr("agents.llm.ollama._service_setting", fake_settings())
    provider = OllamaProvider()
    assert (provider.base_url, provider.model) == ("http://from-service:11434", "service-model")


def test_default_resolver_delegates_to_configuration_service(monkeypatch):
    fake_module = types.ModuleType("configuration.services")
    fake_module.get_setting = lambda name: f"value:{name}"
    monkeypatch.setitem(sys.modules, "configuration.services", fake_module)

    assert _service_setting("ollama_base_url") == "value:ollama_base_url"
    assert _service_setting("ollama_model") == "value:ollama_model"


@pytest.mark.django_db
def test_runtime_override_takes_effect_without_restart():
    """A DB override is read per construction, so no process restart is needed."""
    from configuration.models import AppSettings
    from configuration.services import invalidate_settings_cache

    AppSettings.objects.update_or_create(
        pk=1,
        defaults={"ollama_base_url": "http://db-override:11434", "ollama_model": "db-model"},
    )
    invalidate_settings_cache()

    first = OllamaProvider()
    assert (first.base_url, first.model) == ("http://db-override:11434", "db-model")

    # Change the runtime setting; the next provider instance picks it up.
    AppSettings.objects.filter(pk=1).update(ollama_model="db-model-2")
    invalidate_settings_cache()

    assert OllamaProvider().model == "db-model-2"
    # An explicitly constructed provider still ignores the DB override.
    assert OllamaProvider(model="explicit").model == "explicit"


def test_schema_to_json_schema_accepts_model_and_dict():
    from_model = LLMProvider.schema_to_json_schema(ModelSpecification)
    assert from_model["title"] == "ModelSpecification"
    assert LLMProvider.schema_to_json_schema({"type": "object"}) == {"type": "object"}
    with pytest.raises(TypeError):
        LLMProvider.schema_to_json_schema(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# OllamaProvider transport (stubbed, never hits the network)
# ---------------------------------------------------------------------------


def _capturing_post(
    monkeypatch, result: dict[str, Any]
) -> tuple[OllamaProvider, list[tuple[str, dict[str, Any]]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((path, payload))
        return result

    provider = OllamaProvider(base_url="http://test:11434", model="test-model")
    monkeypatch.setattr(provider, "_post", fake_post)
    return provider, calls


def test_generate_calls_api_generate(monkeypatch):
    provider, calls = _capturing_post(monkeypatch, {"response": "hello world"})
    assert provider.generate("say hi") == "hello world"
    path, payload = calls[0]
    assert path == "/api/generate"
    assert payload == {"model": "test-model", "prompt": "say hi", "stream": False}


def test_generate_forwards_system_and_options(monkeypatch):
    provider, calls = _capturing_post(monkeypatch, {"response": "ok"})
    provider.generate("hi", system="be brief", options={"temperature": 0})
    _, payload = calls[0]
    assert payload["system"] == "be brief"
    assert payload["options"] == {"temperature": 0}


def test_generate_rejects_empty_prompt():
    with pytest.raises(LLMError):
        OllamaProvider(base_url="http://test:11434", model="test-model").generate("   ")


def test_generate_rejects_empty_response(monkeypatch):
    provider, _ = _capturing_post(monkeypatch, {"response": "  "})
    with pytest.raises(LLMError):
        provider.generate("hi")


def test_structured_calls_api_chat_and_validates(monkeypatch):
    provider, calls = _capturing_post(monkeypatch, {"message": {"content": json.dumps(VALID_SPEC)}})
    result = provider.structured("make a phone holder", ModelSpecification)
    path, payload = calls[0]
    assert path == "/api/chat"
    assert payload["format"] == ModelSpecification.to_json_schema()
    assert payload["stream"] is False
    assert payload["messages"][1]["content"] == "make a phone holder"
    assert result == VALID_SPEC


def test_structured_accepts_raw_json_schema(monkeypatch):
    provider, _ = _capturing_post(monkeypatch, {"message": {"content": '{"anything": 1}'}})
    assert provider.structured("hi", {"type": "object"}) == {"anything": 1}


def test_structured_rejects_invalid_json(monkeypatch):
    provider, _ = _capturing_post(monkeypatch, {"message": {"content": "not json"}})
    with pytest.raises(LLMError):
        provider.structured("hi", ModelSpecification)


def test_structured_rejects_schema_mismatch(monkeypatch):
    provider, _ = _capturing_post(monkeypatch, {"message": {"content": '{"object": "x"}'}})
    with pytest.raises(LLMError):
        provider.structured("hi", ModelSpecification)


def test_structured_rejects_missing_message(monkeypatch):
    provider, _ = _capturing_post(monkeypatch, {"done": True})
    with pytest.raises(LLMError):
        provider.structured("hi", ModelSpecification)


def test_connection_error_is_wrapped(monkeypatch):
    def boom(*args: Any, **kwargs: Any) -> None:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("agents.llm.ollama.urllib.request.urlopen", boom)
    provider = OllamaProvider(base_url="http://test:11434", model="test-model")
    with pytest.raises(LLMError, match="Cannot reach Ollama"):
        provider.generate("hi")


def test_http_error_is_wrapped(monkeypatch):
    def boom(*args: Any, **kwargs: Any) -> None:
        raise urllib.error.HTTPError(
            "http://test:11434/api/generate", 500, "boom", {}, io.BytesIO(b"server error")
        )

    monkeypatch.setattr("agents.llm.ollama.urllib.request.urlopen", boom)
    provider = OllamaProvider(base_url="http://test:11434", model="test-model")
    with pytest.raises(LLMError, match="HTTP 500"):
        provider.generate("hi")
