"""Repairable vs. non-repairable LLM failures -- the contract, for both backends.

One distinction, one test module: a response that **arrived but does not fit**
is repairable (``LLMResponseError``), a request that **did not complete** is
not (a plain ``LLMError``). Both backends are held to the same table at the
bottom of this file, so the behaviour cannot silently become Ollama-specific.

No network, no database, no OpenSCAD: the Ollama transport is stubbed at
``_post``/``urlopen`` and the OpenAI SDK at the injected fake client.
"""

from __future__ import annotations

import copy
import io
import json
import urllib.error
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from agents.llm import LLMError, LLMResponseError, OllamaProvider, OpenAICompatibleProvider
from agents.spec import ModelSpecification

VALID_SPEC: dict[str, Any] = ModelSpecification.example()

#: A payload that parses as JSON, is an object, and does **not** satisfy the
#: schema -- the failure the 13-model round measured most often (an ``extrude``
#: with an empty profile, a ``depth`` under the schema minimum).
SCHEMA_VIOLATION: dict[str, Any] = {**copy.deepcopy(VALID_SPEC), "wall_thickness": 0.1}
SCHEMA_VIOLATION_JSON = json.dumps(SCHEMA_VIOLATION)


# ---------------------------------------------------------------------------
# The type itself
# ---------------------------------------------------------------------------


def test_response_error_is_an_llm_error() -> None:
    """Every existing ``except LLMError`` must keep catching it."""
    assert issubclass(LLMResponseError, LLMError)
    assert LLMResponseError("nope").validation_error is None


def test_existing_handler_catches_the_repairable_case() -> None:
    """A handler written before the subclass existed still sees the failure."""
    caught: list[str] = []
    try:
        raise LLMResponseError("answer did not fit")
    except LLMError as exc:  # noqa: BLE001 - catching broadly is the assertion
        caught.append(str(exc))
    assert caught == ["answer did not fit"]


def test_response_error_carries_the_validation_error_of_a_schema_mismatch() -> None:
    """A bounded repair hint is built from ``exc.errors()``, so the error rides along."""
    try:
        ModelSpecification.model_validate(SCHEMA_VIOLATION)
    except ValidationError as exc:
        error = LLMResponseError("does not match", validation_error=exc)
    assert error.validation_error is not None
    assert [item["loc"] for item in error.validation_error.errors()] == [("wall_thickness",)]


# ---------------------------------------------------------------------------
# Providers under test
# ---------------------------------------------------------------------------


def _ollama(monkeypatch, result: dict[str, Any]) -> OllamaProvider:
    """An ``OllamaProvider`` whose ``_post`` is stubbed (no socket, no Django)."""

    def fake_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return result

    provider = OllamaProvider(
        base_url="http://test:11434",
        model="test-model",
        get_setting=lambda name: None,
    )
    monkeypatch.setattr(provider, "_post", fake_post)
    return provider


def _ollama_socket_failing(monkeypatch, error: Exception) -> OllamaProvider:
    """An ``OllamaProvider`` with the *real* ``_post`` and a failing socket.

    Only the socket is stubbed, so the exception is translated by the production
    transport code rather than injected behind it.
    """

    def boom(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr("agents.llm.ollama.urllib.request.urlopen", boom)
    return OllamaProvider(
        base_url="http://test:11434",
        model="test-model",
        get_setting=lambda name: None,
    )


class _Message:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str | None) -> None:
        self.message = _Message(content)


class _Response:
    def __init__(self, content: str | None) -> None:
        self.choices = [_Choice(content)]


class _Completions:
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


class _Client:
    """Stand-in for ``openai.OpenAI`` exposing only ``chat.completions``."""

    def __init__(self, *responses: Any) -> None:
        self.completions = _Completions(list(responses))
        self.chat = self


def _openai(*responses: Any) -> tuple[OpenAICompatibleProvider, _Client]:
    client = _Client(*responses)
    provider = OpenAICompatibleProvider(
        client=client,
        base_url="http://gateway:8000/v1",
        api_key="sk-test",
        model="test-model",
        get_setting=lambda name: None,
    )
    return provider, client


# ---------------------------------------------------------------------------
# Ollama: repairable
# ---------------------------------------------------------------------------


def test_ollama_invalid_json_is_repairable(monkeypatch) -> None:
    provider = _ollama(monkeypatch, {"message": {"content": "not json at all"}})
    with pytest.raises(LLMResponseError, match="not valid JSON"):
        provider.structured("hi", ModelSpecification)


def test_ollama_non_object_json_is_repairable(monkeypatch) -> None:
    provider = _ollama(monkeypatch, {"message": {"content": "[1, 2, 3]"}})
    with pytest.raises(LLMResponseError, match="must be a JSON object"):
        provider.structured("hi", ModelSpecification)


def test_ollama_schema_mismatch_is_repairable(monkeypatch) -> None:
    provider = _ollama(monkeypatch, {"message": {"content": SCHEMA_VIOLATION_JSON}})
    with pytest.raises(LLMResponseError, match="does not match ModelSpecification") as excinfo:
        provider.structured("hi", ModelSpecification)
    # The full error stays in the message (the operator needs every violation)
    # and the Pydantic error is available for a bounded repair hint.
    assert "greater than or equal to 0.4" in str(excinfo.value)
    assert excinfo.value.validation_error is not None


# ---------------------------------------------------------------------------
# Ollama: not repairable
# ---------------------------------------------------------------------------


def test_ollama_timeout_is_not_repairable(monkeypatch) -> None:
    """Repeating a timeout would only double the wait for the same timeout."""
    provider = _ollama_socket_failing(monkeypatch, TimeoutError("timed out"))
    with pytest.raises(LLMError, match="timed out") as excinfo:
        provider.structured("hi", ModelSpecification)
    assert not isinstance(excinfo.value, LLMResponseError)


def test_ollama_http_error_is_not_repairable(monkeypatch) -> None:
    provider = _ollama_socket_failing(
        monkeypatch,
        urllib.error.HTTPError(
            "http://test:11434/api/chat", 500, "boom", {}, io.BytesIO(b"server error")
        ),
    )
    with pytest.raises(LLMError, match="HTTP 500") as excinfo:
        provider.structured("hi", ModelSpecification)
    assert not isinstance(excinfo.value, LLMResponseError)


def test_ollama_connection_error_is_not_repairable(monkeypatch) -> None:
    provider = _ollama_socket_failing(monkeypatch, urllib.error.URLError("connection refused"))
    with pytest.raises(LLMError, match="Cannot reach Ollama") as excinfo:
        provider.structured("hi", ModelSpecification)
    assert not isinstance(excinfo.value, LLMResponseError)


def test_ollama_broken_http_envelope_is_not_repairable(monkeypatch) -> None:
    """A body that is not an object is a broken envelope, not a bad answer."""

    def respond(*args: Any, **kwargs: Any) -> io.BytesIO:
        return io.BytesIO(b"<html>gateway error</html>")

    monkeypatch.setattr("agents.llm.ollama.urllib.request.urlopen", lambda *a, **k: respond())
    provider = OllamaProvider(
        base_url="http://test:11434", model="test-model", get_setting=lambda name: None
    )
    with pytest.raises(LLMError, match="invalid JSON") as excinfo:
        provider.structured("hi", ModelSpecification)
    assert not isinstance(excinfo.value, LLMResponseError)


def test_ollama_empty_prompt_is_not_repairable() -> None:
    provider = OllamaProvider(
        base_url="http://test:11434", model="test-model", get_setting=lambda name: None
    )
    with pytest.raises(LLMError, match="non-empty prompt") as excinfo:
        provider.structured("   ", ModelSpecification)
    assert not isinstance(excinfo.value, LLMResponseError)


def test_ollama_empty_generate_response_is_not_repairable(monkeypatch) -> None:
    provider = _ollama(monkeypatch, {"response": "   "})
    with pytest.raises(LLMError, match="empty generate response") as excinfo:
        provider.generate("hi")
    assert not isinstance(excinfo.value, LLMResponseError)


def test_ollama_missing_message_content_is_not_repairable(monkeypatch) -> None:
    provider = _ollama(monkeypatch, {"done": True})
    with pytest.raises(LLMError, match="no message content") as excinfo:
        provider.structured("hi", ModelSpecification)
    assert not isinstance(excinfo.value, LLMResponseError)


# ---------------------------------------------------------------------------
# OpenAI-compatible: the same table
# ---------------------------------------------------------------------------


def test_openai_invalid_json_is_repairable() -> None:
    provider, _ = _openai(_Response("not json at all"))
    with pytest.raises(LLMResponseError, match="not valid JSON"):
        provider.structured("hi", ModelSpecification)


def test_openai_non_object_json_is_repairable() -> None:
    provider, _ = _openai(_Response("[1, 2, 3]"))
    with pytest.raises(LLMResponseError, match="must be a JSON object"):
        provider.structured("hi", {"type": "array"})


def test_openai_schema_mismatch_is_repairable() -> None:
    provider, _ = _openai(_Response(SCHEMA_VIOLATION_JSON))
    with pytest.raises(LLMResponseError, match="does not match ModelSpecification") as excinfo:
        provider.structured("hi", ModelSpecification)
    assert "greater than or equal to 0.4" in str(excinfo.value)
    assert excinfo.value.validation_error is not None


def test_openai_timeout_is_not_repairable() -> None:
    provider, _ = _openai(TimeoutError("timed out"))
    with pytest.raises(LLMError, match="timed out") as excinfo:
        provider.structured("hi", ModelSpecification)
    assert not isinstance(excinfo.value, LLMResponseError)


def test_openai_http_error_is_not_repairable() -> None:
    # The SDK raises one exception class for an HTTP status and for a timeout,
    # so they cannot be told apart here -- and neither is retried, which is the
    # right outcome for both.
    provider, _ = _openai(RuntimeError("Error code: 429 - rate limit reached"))
    with pytest.raises(LLMError, match="429") as excinfo:
        provider.structured("hi", ModelSpecification)
    assert not isinstance(excinfo.value, LLMResponseError)


def test_openai_empty_prompt_is_not_repairable() -> None:
    provider, client = _openai()
    with pytest.raises(LLMError, match="non-empty prompt") as excinfo:
        provider.structured("   ", ModelSpecification)
    assert not isinstance(excinfo.value, LLMResponseError)
    assert client.completions.calls == []


def test_openai_empty_generate_response_is_not_repairable() -> None:
    provider, _ = _openai(_Response("   "))
    with pytest.raises(LLMError, match="empty generate response") as excinfo:
        provider.generate("hi")
    assert not isinstance(excinfo.value, LLMResponseError)


def test_openai_missing_message_content_is_not_repairable() -> None:
    provider, _ = _openai(_Response(None))
    with pytest.raises(LLMError, match="no message content") as excinfo:
        provider.structured("hi", ModelSpecification)
    assert not isinstance(excinfo.value, LLMResponseError)


# ---------------------------------------------------------------------------
# The whole table, both backends, one assertion per row
# ---------------------------------------------------------------------------

_CALL_STRUCTURED = "structured"
_CALL_GENERATE = "generate"
_CALL_EMPTY_PROMPT = "empty prompt"

#: case -> (build Ollama, build OpenAI, which call, repairable?)
_TABLE: dict[str, tuple[Callable[..., Any], Callable[[], Any], str, bool]] = {
    "answer is not valid JSON": (
        lambda mp: _ollama(mp, {"message": {"content": "not json"}}),
        lambda: _openai(_Response("not json"))[0],
        _CALL_STRUCTURED,
        True,
    ),
    "answer is valid JSON but not an object": (
        lambda mp: _ollama(mp, {"message": {"content": "[1, 2, 3]"}}),
        lambda: _openai(_Response("[1, 2, 3]"))[0],
        _CALL_STRUCTURED,
        True,
    ),
    "answer does not match the schema": (
        lambda mp: _ollama(mp, {"message": {"content": SCHEMA_VIOLATION_JSON}}),
        lambda: _openai(_Response(SCHEMA_VIOLATION_JSON))[0],
        _CALL_STRUCTURED,
        True,
    ),
    "empty prompt (caller mistake)": (
        lambda mp: _ollama(mp, {"message": {"content": SCHEMA_VIOLATION_JSON}}),
        lambda: _openai(_Response(SCHEMA_VIOLATION_JSON))[0],
        _CALL_EMPTY_PROMPT,
        False,
    ),
    "empty generate response": (
        lambda mp: _ollama(mp, {"response": "   "}),
        lambda: _openai(_Response("   "))[0],
        _CALL_GENERATE,
        False,
    ),
    "no message content": (
        lambda mp: _ollama(mp, {"done": True}),
        lambda: _openai(_Response(None))[0],
        _CALL_STRUCTURED,
        False,
    ),
    "timeout": (
        lambda mp: _ollama_socket_failing(mp, TimeoutError("timed out")),
        lambda: _openai(TimeoutError("timed out"))[0],
        _CALL_STRUCTURED,
        False,
    ),
    "HTTP error": (
        lambda mp: _ollama_socket_failing(
            mp,
            urllib.error.HTTPError("http://t/api/chat", 500, "boom", {}, io.BytesIO(b"e")),
        ),
        lambda: _openai(RuntimeError("Error code: 500 - server error"))[0],
        _CALL_STRUCTURED,
        False,
    ),
}


def _drive(provider: Any, call: str) -> Any:
    if call == _CALL_GENERATE:
        return provider.generate("hi")
    if call == _CALL_EMPTY_PROMPT:
        return provider.structured("   ", ModelSpecification)
    return provider.structured("hi", ModelSpecification)


@pytest.mark.parametrize("case", sorted(_TABLE))
def test_both_backends_agree_on_the_classification(monkeypatch, case: str) -> None:
    """Same case, same classification, whichever backend is configured."""
    build_ollama, build_openai, call, repairable = _TABLE[case]
    for provider in (build_ollama(monkeypatch), build_openai()):
        with pytest.raises(LLMError) as excinfo:
            _drive(provider, call)
        assert isinstance(excinfo.value, LLMResponseError) is repairable, case
