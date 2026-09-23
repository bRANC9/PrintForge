"""Provider contract, factory and Ollama transport tests (no network)."""

from __future__ import annotations

import base64
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
    normalise_images,
)
from agents.llm.ollama import DEFAULT_TIMEOUT_SEC, _service_setting
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


def test_get_provider_returns_openai_provider():
    provider = get_provider(
        "openai-compatible",
        base_url="http://gateway:8000/v1/",
        api_key="sk-test",
        model="local-model",
        get_setting=lambda name: None,
    )
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.base_url == "http://gateway:8000/v1"
    assert provider.model == "local-model"


def test_get_provider_rejects_unknown_name():
    with pytest.raises(LLMError):
        get_provider("does-not-exist")


# ---------------------------------------------------------------------------
# Factory runtime provider resolution (injected seam / monkeypatch, no DB)
# ---------------------------------------------------------------------------


def test_get_provider_uses_injected_seam_for_runtime_override():
    """A runtime ``llm_provider`` override selects the backend (seam injected)."""
    provider = get_provider(
        get_setting=fake_settings(
            {
                "llm_provider": "openai-compatible",
                "openai_base_url": "http://gateway:8000/v1/",
                "openai_api_key": "sk-test",
                "openai_model": "local-model",
            }
        )
    )
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.name == "openai-compatible"
    assert provider.base_url == "http://gateway:8000/v1"
    assert provider.model == "local-model"


def test_get_provider_reads_llm_provider_from_settings_service(monkeypatch):
    """Without an explicit name the factory asks the runtime settings service."""
    monkeypatch.setattr(
        "agents.llm.factory._service_setting",
        lambda name: "openai-compatible" if name == "llm_provider" else None,
    )
    # The constructed provider resolves its own settings; stub it so no DB is hit.
    monkeypatch.setattr("agents.llm.openai._service_setting", lambda name: None)

    provider = get_provider()
    assert isinstance(provider, OpenAICompatibleProvider)


def test_get_provider_runtime_override_accepts_openai_alias(monkeypatch):
    monkeypatch.setattr(
        "agents.llm.factory._service_setting",
        lambda name: "openai" if name == "llm_provider" else None,
    )
    monkeypatch.setattr("agents.llm.openai._service_setting", lambda name: None)

    assert isinstance(get_provider(), OpenAICompatibleProvider)


def test_get_provider_runtime_override_beats_django_setting(monkeypatch, settings):
    settings.LLM_PROVIDER = "ollama"
    monkeypatch.setattr(
        "agents.llm.factory._service_setting",
        lambda name: "openai-compatible" if name == "llm_provider" else None,
    )
    monkeypatch.setattr("agents.llm.openai._service_setting", lambda name: None)

    assert isinstance(get_provider(), OpenAICompatibleProvider)


def test_get_provider_explicit_name_wins_over_runtime_override(monkeypatch):
    monkeypatch.setattr("agents.llm.factory._service_setting", lambda name: "openai-compatible")
    monkeypatch.setattr("agents.llm.ollama._service_setting", lambda name: None)

    provider = get_provider("ollama")
    assert isinstance(provider, OllamaProvider)


def test_get_provider_falls_back_to_django_setting_when_service_fails(monkeypatch, settings):
    settings.LLM_PROVIDER = "openai-compatible"

    def broken(name: str) -> Any:
        raise RuntimeError("settings service unavailable")

    monkeypatch.setattr("agents.llm.factory._service_setting", broken)
    monkeypatch.setattr("agents.llm.openai._service_setting", lambda name: None)

    assert isinstance(get_provider(), OpenAICompatibleProvider)


def test_get_provider_falls_back_to_ollama_when_unconfigured(monkeypatch, settings):
    settings.LLM_PROVIDER = ""
    monkeypatch.setattr("agents.llm.factory._service_setting", lambda name: None)
    monkeypatch.setattr("agents.llm.ollama._service_setting", lambda name: None)

    provider = get_provider()
    assert isinstance(provider, OllamaProvider)
    assert provider.name == "ollama"


def test_factory_service_setting_delegates_to_configuration_service(monkeypatch):
    """The lazy seam reads ``configuration.services.get_setting`` (no DB here)."""
    fake_module = types.ModuleType("configuration.services")
    fake_module.get_setting = lambda name: f"value:{name}"
    monkeypatch.setitem(sys.modules, "configuration.services", fake_module)

    from agents.llm.factory import _service_setting as factory_service_setting

    assert factory_service_setting("llm_provider") == "value:llm_provider"


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


# ---------------------------------------------------------------------------
# OllamaProvider timeout resolution (injected seam, no network)
# ---------------------------------------------------------------------------


def test_timeout_defaults_when_setting_missing():
    provider = OllamaProvider(get_setting=fake_settings())
    assert provider.timeout == DEFAULT_TIMEOUT_SEC
    assert isinstance(provider.timeout, float)


def test_timeout_uses_runtime_setting():
    provider = OllamaProvider(get_setting=fake_settings({"ollama_timeout": 45}))
    assert provider.timeout == 45.0


def test_timeout_accepts_numeric_string_setting():
    provider = OllamaProvider(get_setting=fake_settings({"ollama_timeout": "90"}))
    assert provider.timeout == 90.0


def test_explicit_timeout_wins_over_setting():
    provider = OllamaProvider(timeout=7, get_setting=fake_settings({"ollama_timeout": 45}))
    assert provider.timeout == 7.0


def test_explicit_timeout_wins_over_raising_seam():
    def exploding(name: str) -> Any:
        raise AssertionError(f"settings service must not be queried for {name}")

    provider = OllamaProvider(
        base_url="http://test:11434",
        model="test-model",
        timeout=7,
        get_setting=exploding,
    )
    assert provider.timeout == 7.0


def test_timeout_positional_argument_still_supported():
    provider = OllamaProvider(None, None, 5, get_setting=fake_settings())
    assert provider.timeout == 5.0


def test_timeout_degrades_when_seam_raises():
    def broken(name: str) -> Any:
        if name == "ollama_timeout":
            raise RuntimeError("settings service unavailable")
        return None

    provider = OllamaProvider(get_setting=broken)
    assert provider.timeout == DEFAULT_TIMEOUT_SEC


def test_timeout_degrades_when_setting_unknown():
    def unknown(name: str) -> Any:
        if name == "ollama_timeout":
            raise ValueError(f"Unknown setting: {name}")
        return None

    provider = OllamaProvider(get_setting=unknown)
    assert provider.timeout == DEFAULT_TIMEOUT_SEC


@pytest.mark.parametrize(
    "bad",
    [0, -5, "0", "-1", "", "   ", "abc", None, float("nan"), float("inf")],
)
def test_invalid_timeout_setting_falls_back_to_default(bad):
    provider = OllamaProvider(get_setting=fake_settings({"ollama_timeout": bad}))
    assert provider.timeout == DEFAULT_TIMEOUT_SEC


def test_invalid_explicit_timeout_falls_back_to_default():
    provider = OllamaProvider(timeout=0, get_setting=fake_settings({"ollama_timeout": 45}))
    assert provider.timeout == DEFAULT_TIMEOUT_SEC


def test_reload_picks_up_timeout_change():
    live = {
        "ollama_base_url": "http://old:11434",
        "ollama_model": "old-model",
        "ollama_timeout": 30,
    }
    provider = OllamaProvider(get_setting=lambda name: live[name])
    assert provider.timeout == 30.0

    live["ollama_timeout"] = 60
    provider.reload()
    assert provider.timeout == 60.0


def test_reload_keeps_explicit_timeout():
    live = {
        "ollama_base_url": "http://svc:11434",
        "ollama_model": "svc-model",
        "ollama_timeout": 30,
    }
    provider = OllamaProvider(timeout=7, get_setting=lambda name: live[name])
    live["ollama_timeout"] = 60
    provider.reload()
    assert provider.timeout == 7.0


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


# ---------------------------------------------------------------------------
# Vision support (terv.md 27. fejezet)
# ---------------------------------------------------------------------------

VISION_IMAGE = b"\x89PNG\r\n\x1a\nfake-image-bytes"


def test_base_supports_vision_defaults_to_false():
    assert FakeProvider().supports_vision() is False


def test_normalise_images_accepts_buffers_and_iterables():
    assert normalise_images(None) == []
    assert normalise_images(b"x") == [b"x"]
    assert normalise_images(bytearray(b"xy")) == [b"xy"]
    assert normalise_images(memoryview(b"abc")) == [b"abc"]
    assert normalise_images([b"a", bytearray(b"b"), memoryview(b"c")]) == [b"a", b"b", b"c"]


@pytest.mark.parametrize("bad", ["str", 3, [b"ok", "nope"], ["nope"]])
def test_normalise_images_rejects_unsupported_types(bad):
    with pytest.raises(TypeError):
        normalise_images(bad)


def test_normalise_images_rejects_empty_bytes():
    with pytest.raises(ValueError):
        normalise_images(b"")


def _vision_provider(
    monkeypatch, *, capabilities: list[str], unique: str
) -> tuple[OllamaProvider, list[tuple[str, dict[str, Any]]]]:
    """Provider whose stubbed ``_post`` serves ``/api/show`` and generation."""
    provider = OllamaProvider(base_url=f"http://vision-{unique}:11434", model=f"m-{unique}")
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((path, payload))
        if path == "/api/show":
            return {"capabilities": capabilities}
        return {"response": "seen", "message": {"content": json.dumps(VALID_SPEC)}}

    monkeypatch.setattr(provider, "_post", fake_post)
    return provider, calls


def test_ollama_supports_vision_reads_capabilities(monkeypatch):
    provider, calls = _vision_provider(
        monkeypatch, capabilities=["completion", "vision"], unique="a"
    )
    assert provider.supports_vision() is True
    assert calls[0] == ("/api/show", {"model": "m-a"})


def test_ollama_supports_vision_ignores_unrelated_capabilities(monkeypatch):
    provider, _ = _vision_provider(monkeypatch, capabilities=["completion"], unique="b")
    assert provider.supports_vision() is False


def test_ollama_supports_vision_missing_capabilities_is_false(monkeypatch):
    provider = OllamaProvider(base_url="http://vision-c:11434", model="m-c")
    monkeypatch.setattr(provider, "_post", lambda path, payload: {"model": "m-c"})
    assert provider.supports_vision() is False


def test_ollama_supports_vision_failure_is_false_and_not_cached(monkeypatch):
    provider = OllamaProvider(base_url="http://vision-d:11434", model="m-d")
    seen: list[str] = []

    def flaky(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(path)
        if len(seen) == 1:
            raise LLMError("show unavailable")
        return {"capabilities": ["vision"]}

    monkeypatch.setattr(provider, "_post", flaky)
    assert provider.supports_vision() is False
    assert provider.supports_vision() is True  # failure was not cached
    assert seen.count("/api/show") == 2


def test_ollama_supports_vision_is_cached(monkeypatch):
    provider, calls = _vision_provider(monkeypatch, capabilities=["vision"], unique="e")
    assert provider.supports_vision() is True
    assert provider.supports_vision() is True
    assert [path for path, _ in calls] == ["/api/show"]


def test_ollama_generate_sends_base64_images(monkeypatch):
    provider, calls = _vision_provider(monkeypatch, capabilities=["vision"], unique="f")
    assert provider.generate("what is this", images=[VISION_IMAGE]) == "seen"
    assert calls[-1][1]["images"] == [base64.b64encode(VISION_IMAGE).decode("ascii")]


def test_ollama_generate_rejects_images_without_vision(monkeypatch):
    provider, calls = _vision_provider(monkeypatch, capabilities=["completion"], unique="g")
    with pytest.raises(LLMError, match="does not support vision"):
        provider.generate("hi", images=[VISION_IMAGE])
    assert [path for path, _ in calls] == ["/api/show"]


def test_ollama_structured_sends_images_on_user_message(monkeypatch):
    provider, calls = _vision_provider(monkeypatch, capabilities=["vision"], unique="h")
    provider.structured("make it", ModelSpecification, images=[VISION_IMAGE])
    user_message = calls[-1][1]["messages"][1]
    assert user_message["images"] == [base64.b64encode(VISION_IMAGE).decode("ascii")]


def test_ollama_structured_rejects_images_without_vision(monkeypatch):
    provider, _ = _vision_provider(monkeypatch, capabilities=["completion"], unique="i")
    with pytest.raises(LLMError, match="does not support vision"):
        provider.structured("hi", ModelSpecification, images=[VISION_IMAGE])


def test_ollama_without_images_does_not_query_show(monkeypatch):
    provider, calls = _vision_provider(monkeypatch, capabilities=["vision"], unique="j")
    assert provider.generate("hi") == "seen"
    assert [path for path, _ in calls] == ["/api/generate"]


def test_openai_supports_vision_is_config_driven():
    off = OpenAICompatibleProvider(get_setting=lambda name: None)
    assert off.supports_vision() is False
    on = OpenAICompatibleProvider(vision=True, get_setting=lambda name: None)
    assert on.supports_vision() is True
