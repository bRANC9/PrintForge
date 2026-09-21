"""Provider contract, factory and Ollama transport tests (no network)."""

from __future__ import annotations

import io
import json
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
from agents.spec import ModelSpecification

VALID_SPEC: dict[str, Any] = ModelSpecification.example()


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
    provider = get_provider()
    assert isinstance(provider, OllamaProvider)
    assert provider.name == "ollama"


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
        OllamaProvider().generate("   ")


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
    provider = OllamaProvider(base_url="http://test:11434")
    with pytest.raises(LLMError, match="Cannot reach Ollama"):
        provider.generate("hi")


def test_http_error_is_wrapped(monkeypatch):
    def boom(*args: Any, **kwargs: Any) -> None:
        raise urllib.error.HTTPError(
            "http://test:11434/api/generate", 500, "boom", {}, io.BytesIO(b"server error")
        )

    monkeypatch.setattr("agents.llm.ollama.urllib.request.urlopen", boom)
    provider = OllamaProvider(base_url="http://test:11434")
    with pytest.raises(LLMError, match="HTTP 500"):
        provider.generate("hi")
