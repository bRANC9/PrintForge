"""OpenAI-compatible provider tests with a fake SDK client (no network).

The provider is constructed with an injected ``client`` so ``openai`` is never
imported for transport ````and no HTTP request is ever made. The fake mirrors
only the tiny slice of the SDK surface the provider touches:
``client.chat.completions.create(**request)``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agents.llm import LLMError, OpenAICompatibleProvider
from agents.spec import ModelSpecification

VALID_SPEC: dict[str, Any] = ModelSpecification.example()

SERVICE_VALUES: dict[str, str] = {
    "openai_base_url": "http://from-service:8000/v1/",
    "openai_api_key": "sk-from-service",
    "openai_model": "service-model",
}


def fake_settings(values: dict[str, Any] | None = None):
    """Return a ``get_setting`` seam backed by a plain dict (no DB, no Django)."""
    data = SERVICE_VALUES if values is None else values

    def get_setting(name: str) -> Any:
        return data.get(name)

    return get_setting


# ---------------------------------------------------------------------------
# Minimal fake SDK
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str | None, *, as_dict: bool = False) -> None:
        choice: Any = {"message": {"content": content}} if as_dict else _FakeChoice(content)
        self.choices = [choice]


class _FakeCompletions:
    def __init__(self, queue: list[Any]) -> None:
        self.queue = queue
        self.calls: list[dict[str, Any]] = []

    def create(self, **request: Any) -> Any:
        self.calls.append(request)
        if not self.queue:
            raise AssertionError("fake client ran out of queued responses")
        result = self.queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _FakeChat:
    def __init__(self, queue: list[Any]) -> None:
        self.completions = _FakeCompletions(queue)


class FakeClient:
    """Stand-in for ``openai.OpenAI`` exposing only ``chat.completions``."""

    def __init__(self, *responses: Any) -> None:
        self.chat = _FakeChat(list(responses))

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.chat.completions.calls


def provider_with(fake: FakeClient, **kwargs: Any) -> OpenAICompatibleProvider:
    """Provider wired to *fake* with an injected settings seam."""
    options: dict[str, Any] = {
        "client": fake,
        "base_url": "http://gateway:8000/v1",
        "api_key": "sk-test",
        "model": "test-model",
        "get_setting": lambda name: None,
    }
    options.update(kwargs)
    return OpenAICompatibleProvider(**options)


# ---------------------------------------------------------------------------
# Settings resolution
# ---------------------------------------------------------------------------


def test_settings_are_resolved_from_injected_service():
    provider = OpenAICompatibleProvider(get_setting=fake_settings())
    assert provider.base_url == "http://from-service:8000/v1"
    assert provider.api_key == "sk-from-service"
    assert provider.model == "service-model"


def test_explicit_values_win_over_settings_service():
    def exploding(name: str) -> Any:
        raise AssertionError(f"settings service must not be queried for {name}")

    provider = OpenAICompatibleProvider(
        base_url="http://explicit:8000/v1/",
        api_key="sk-explicit",
        model="explicit-model",
        get_setting=exploding,
    )
    assert provider.base_url == "http://explicit:8000/v1"
    assert provider.api_key == "sk-explicit"
    assert provider.model == "explicit-model"


def test_blank_settings_fall_back_to_defaults():
    provider = OpenAICompatibleProvider(
        get_setting=fake_settings(
            {"openai_base_url": "  ", "openai_api_key": None, "openai_model": ""}
        )
    )
    assert provider.base_url == ""
    assert provider.api_key == ""
    assert provider.model == "gpt-4o-mini"


def test_model_falls_back_to_django_settings(monkeypatch, settings):
    settings.OPENAI_MODEL = "django-model"
    provider = OpenAICompatibleProvider(get_setting=lambda name: None)
    assert provider.model == "django-model"


def test_model_falls_back_to_settings_when_service_raises(settings):
    # ``openai_model`` is now a registered runtime setting; when the settings
    # service raises (unknown name / unreachable), the provider falls back to
    # Django settings.
    settings.OPENAI_MODEL = "settings-model"

    def broken(name: str) -> Any:
        # base_url/api_key resolve normally; the model name raises.
        if name == "openai_model":
            raise ValueError(f"Unknown setting: {name}")
        return None

    provider = OpenAICompatibleProvider(get_setting=broken)
    assert provider.model == "settings-model"


def test_reload_picks_up_runtime_settings_change():
    live = {
        "openai_base_url": "http://old:8000/v1",
        "openai_api_key": "sk-old",
        "openai_model": "old-model",
    }
    provider = OpenAICompatibleProvider(get_setting=lambda name: live[name])
    assert (provider.base_url, provider.api_key, provider.model) == (
        "http://old:8000/v1",
        "sk-old",
        "old-model",
    )

    live.update(
        openai_base_url="http://new:8000/v1",
        openai_api_key="sk-new",
        openai_model="new-model",
    )
    provider.reload()
    assert (provider.base_url, provider.api_key, provider.model) == (
        "http://new:8000/v1",
        "sk-new",
        "new-model",
    )


def test_reload_keeps_explicit_values():
    live = {
        "openai_base_url": "http://svc:8000/v1",
        "openai_api_key": "sk-svc",
        "openai_model": "svc-model",
    }
    provider = OpenAICompatibleProvider(get_setting=lambda name: live[name])
    provider.reload(model="override")
    assert provider.model == "override"
    assert provider.base_url == "http://svc:8000/v1"


def test_api_key_is_never_exposed_in_repr():
    provider = OpenAICompatibleProvider(
        api_key="sk-super-secret",
        model="m",
        get_setting=lambda name: None,
    )
    assert "sk-super-secret" not in repr(provider)


# ---------------------------------------------------------------------------
# generate()
# ---------------------------------------------------------------------------


def test_generate_returns_message_content():
    fake = FakeClient(_FakeResponse("hello world"))
    provider = provider_with(fake)
    assert provider.generate("say hi") == "hello world"
    request = fake.calls[0]
    assert request["model"] == "test-model"
    assert request["messages"] == [{"role": "user", "content": "say hi"}]
    assert "response_format" not in request


def test_generate_forwards_system_and_extra_kwargs():
    fake = FakeClient(_FakeResponse("ok"))
    provider = provider_with(fake)
    provider.generate("hi", system="be brief", temperature=0, max_tokens=64)
    request = fake.calls[0]
    assert request["messages"][0] == {"role": "system", "content": "be brief"}
    assert request["temperature"] == 0
    assert request["max_tokens"] == 64


def test_generate_honours_per_call_model_override():
    fake = FakeClient(_FakeResponse("ok"))
    provider = provider_with(fake)
    provider.generate("hi", model="other-model")
    assert fake.calls[0]["model"] == "other-model"


def test_generate_rejects_empty_prompt():
    fake = FakeClient()
    provider = provider_with(fake)
    with pytest.raises(LLMError):
        provider.generate("   ")
    assert fake.calls == []


def test_generate_rejects_empty_response():
    fake = FakeClient(_FakeResponse("   "))
    provider = provider_with(fake)
    with pytest.raises(LLMError):
        provider.generate("hi")


def test_generate_rejects_missing_choices():
    fake = FakeClient(_FakeResponse(None))
    provider = provider_with(fake)
    with pytest.raises(LLMError):
        provider.generate("hi")


# ---------------------------------------------------------------------------
# structured()
# ---------------------------------------------------------------------------


def test_structured_uses_json_schema_response_format_and_validates():
    fake = FakeClient(_FakeResponse(json.dumps(VALID_SPEC)))
    provider = provider_with(fake)
    result = provider.structured("make a phone holder", ModelSpecification)
    assert result == VALID_SPEC

    request = fake.calls[0]
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["name"] == "ModelSpecification"
    assert request["response_format"]["json_schema"]["schema"] == (
        ModelSpecification.to_json_schema()
    )
    assert request["messages"][1] == {"role": "user", "content": "make a phone holder"}


def test_structured_accepts_raw_json_schema():
    fake = FakeClient(_FakeResponse('{"anything": 1}'))
    provider = provider_with(fake)
    assert provider.structured("hi", {"type": "object"}) == {"anything": 1}
    assert fake.calls[0]["response_format"]["json_schema"]["schema"] == {"type": "object"}


def test_structured_accepts_dict_shaped_sdk_response():
    fake = FakeClient(_FakeResponse(json.dumps(VALID_SPEC), as_dict=True))
    provider = provider_with(fake)
    assert provider.structured("hi", ModelSpecification) == VALID_SPEC


def test_structured_rejects_invalid_json():
    fake = FakeClient(_FakeResponse("not json"))
    provider = provider_with(fake)
    with pytest.raises(LLMError):
        provider.structured("hi", ModelSpecification)


def test_structured_rejects_schema_mismatch():
    fake = FakeClient(_FakeResponse('{"object": "x"}'))
    provider = provider_with(fake)
    with pytest.raises(LLMError):
        provider.structured("hi", ModelSpecification)


def test_structured_rejects_non_object_json():
    fake = FakeClient(_FakeResponse("[1, 2, 3]"))
    provider = provider_with(fake)
    with pytest.raises(LLMError):
        provider.structured("hi", {"type": "array"})


def test_structured_rejects_empty_prompt():
    fake = FakeClient()
    provider = provider_with(fake)
    with pytest.raises(LLMError):
        provider.structured("  ", ModelSpecification)


# ---------------------------------------------------------------------------
# Error mapping (no SDK-specific exception classes needed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("connection refused"),
        TimeoutError("timed out"),
        ValueError("bad request"),
        Exception(),
    ],
)
def test_sdk_errors_are_mapped_to_llm_error(error):
    fake = FakeClient(error)
    provider = provider_with(fake)
    with pytest.raises(LLMError):
        provider.generate("hi")


def test_error_message_does_not_leak_the_api_key():
    fake = FakeClient(RuntimeError("unauthorized"))
    provider = provider_with(fake, api_key="sk-super-secret")
    with pytest.raises(LLMError) as excinfo:
        provider.generate("hi")
    assert "sk-super-secret" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Vision (terv.md 27. fejezet)
# ---------------------------------------------------------------------------

VISION_IMAGE = b"\x89PNG\r\n\x1a\nfake-image-bytes"


def test_supports_vision_defaults_to_false():
    assert provider_with(FakeClient()).supports_vision() is False


def test_supports_vision_reflects_the_configured_flag():
    assert provider_with(FakeClient(), vision=True).supports_vision() is True


def test_generate_sends_images_as_data_uris():
    fake = FakeClient(_FakeResponse("seen"))
    provider = provider_with(fake, vision=True)
    assert provider.generate("what is this", images=[VISION_IMAGE]) == "seen"
    content = fake.calls[0]["messages"][-1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "what is this"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_generate_rejects_images_without_vision():
    fake = FakeClient()
    provider = provider_with(fake)
    with pytest.raises(LLMError, match="not configured for vision"):
        provider.generate("hi", images=[VISION_IMAGE])
    assert fake.calls == []


def test_structured_sends_images_as_data_uris():
    fake = FakeClient(_FakeResponse(json.dumps(VALID_SPEC)))
    provider = provider_with(fake, vision=True)
    provider.structured("make it", ModelSpecification, images=[VISION_IMAGE])
    content = fake.calls[0]["messages"][-1]["content"]
    assert content[1]["type"] == "image_url"


def test_structured_rejects_images_without_vision():
    fake = FakeClient()
    provider = provider_with(fake)
    with pytest.raises(LLMError, match="not configured for vision"):
        provider.structured("hi", ModelSpecification, images=[VISION_IMAGE])
    assert fake.calls == []
