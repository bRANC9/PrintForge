"""Planner node tests: the bounded schema-repair re-ask.

No network, no database, no OpenSCAD: a fake provider returns an invalid plan
once and then a valid one, and the test inspects what the second call was told.

The re-ask fires for two shapes of "the answer did not fit" -- a
:class:`~agents.llm.LLMResponseError` the provider raised while validating the
model's own answer, and a :class:`~pydantic.ValidationError` from
``coerce_plan`` -- and for neither of the transport-shaped failures that used to
share a handler with it.
"""

from __future__ import annotations

import copy
import io
import json
import urllib.error
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from agents.graph.planner import (
    MAX_REPAIR_ERRORS,
    MAX_REPAIR_HINT_CHARS,
    PlannerPlan,
    _repair_hint,
    make_planner_node,
)
from agents.graph.tests.fakes import DEFAULT_SPEC
from agents.llm import LLMError, LLMResponseError, OllamaProvider, OpenAICompatibleProvider


def invalid_plan_payload(*, error_count: int = 1) -> dict[str, Any]:
    """Return a plan that fails validation with *error_count* violations.

    Each violation is a distinct field with a distinct message, so a multi-error
    payload is easy to build and easy to assert on.
    """
    plan: dict[str, Any] = copy.deepcopy(DEFAULT_SPEC and {"specification": DEFAULT_SPEC})
    specification = plan["specification"]
    fields: list[tuple[str, Any]] = [
        ("wall_thickness", 0.1),
        ("angle", -5.0),
        ("material", ""),
        ("object", ""),
        ("mounting", {"type": "", "count": -1}),
        ("dimensions", {"width": 0, "height": 0, "thickness": 0}),
    ]
    for key, value in fields[:error_count]:
        specification[key] = value
    return plan


class _FlakyPlannerProvider:
    """Returns an invalid plan for the first *failures* calls, then a good one."""

    name = "flaky"

    def __init__(
        self,
        *,
        failures: int,
        plan: dict[str, Any] | None = None,
        recovered: dict[str, Any] | None = None,
    ) -> None:
        self.failures = failures
        self.plan = plan or {"specification": DEFAULT_SPEC}
        #: What the model returns once it has repaired itself.
        self.recovered = recovered or {"specification": DEFAULT_SPEC}
        self.prompts: list[str] = []

    def supports_vision(self) -> bool:
        return False

    def generate(self, prompt: str, **kwargs: Any) -> str:
        raise AssertionError("the planner must not call generate()")

    def structured(
        self,
        prompt: str,
        schema: type[BaseModel] | dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.prompts.append(prompt)
        if len(self.prompts) <= self.failures:
            try:
                PlannerPlan.model_validate(self.plan)
            except ValidationError as exc:
                raise exc
            raise AssertionError("the fixture plan was supposed to be invalid")
        return copy.deepcopy(self.recovered)


def _hint_for(payload: dict[str, Any]) -> str:
    try:
        PlannerPlan.model_validate(payload)
    except ValidationError as exc:
        return _repair_hint(exc)
    raise AssertionError("fixture is not invalid")


def _error_count(payload: dict[str, Any]) -> int:
    try:
        PlannerPlan.model_validate(payload)
    except ValidationError as exc:
        return len(exc.errors())
    raise AssertionError("fixture is not invalid")


# ---------------------------------------------------------------------------
# The bound itself
# ---------------------------------------------------------------------------


def test_repair_hint_names_the_field_and_the_constraint() -> None:
    """The hint must carry the problem and the offending location."""
    hint = _hint_for(invalid_plan_payload())
    assert "rejected by the schema" in hint
    assert "wall_thickness" in hint
    # The constraint that was violated is what lets a model repair itself.
    assert "greater than or equal to 0.4" in hint


def test_repair_hint_lists_at_most_max_repair_errors() -> None:
    """A model can only act on a couple of concrete complaints at once."""
    payload = invalid_plan_payload(error_count=5)
    assert _error_count(payload) > MAX_REPAIR_ERRORS
    hint = _hint_for(payload)
    bullet_lines = [line for line in hint.splitlines() if line.startswith("- ")]
    # MAX_REPAIR_ERRORS violations, plus the "+N more" line.
    assert len(bullet_lines) == MAX_REPAIR_ERRORS + 1
    assert f"+{_error_count(payload) - MAX_REPAIR_ERRORS} more schema error(s)" in hint


def test_repair_hint_stays_within_the_character_cap() -> None:
    """A Pydantic error for this schema is far too long to paste verbatim."""
    unbounded = invalid_plan_payload(error_count=6)
    try:
        PlannerPlan.model_validate(unbounded)
    except ValidationError as exc:
        assert len(str(exc)) > MAX_REPAIR_HINT_CHARS, "fixture no longer stresses the cap"
    assert len(_hint_for(unbounded)) <= MAX_REPAIR_HINT_CHARS + len(
        "Your previous answer was rejected by the schema. Fix exactly this problem "
        "and return the complete object again:\n"
    )


def test_repair_hint_never_splits_a_constraint_across_lines() -> None:
    """The truncation happens on a line boundary, so no half-typed bound."""
    hint = _hint_for(invalid_plan_payload(error_count=6))
    if "(truncated)" in hint:
        body = hint.split("again:\n", 1)[1]
        for line in body.splitlines()[:-1]:
            assert line.strip(), "truncation left a dangling, context-free line"


def test_repair_hint_is_bounded_for_a_pathologically_long_message() -> None:
    """Even a single huge error message cannot blow the cap."""

    class _Noisy(BaseModel):
        value: int

    with pytest.raises(ValidationError) as excinfo:
        _Noisy.model_validate({"value": "x" * 10_000})
    # Pydantic embeds the received value in the message; the hint must not.
    hint = _repair_hint(excinfo.value)
    assert "x" * 200 not in hint


# ---------------------------------------------------------------------------
# The re-ask uses it
# ---------------------------------------------------------------------------


def test_reask_repeats_the_question_plus_the_bounded_hint() -> None:
    """The second call carries the original question *and* the complaint."""
    provider = _FlakyPlannerProvider(failures=1, plan=invalid_plan_payload())
    node = make_planner_node(provider=provider, max_attempts=3)

    result = node({"prompt": "make an adapter", "skills": [], "history": []})

    assert result["status"] == "planned"
    assert len(provider.prompts) == 2
    assert provider.prompts[0] == "make an adapter"
    assert provider.prompts[1].startswith("make an adapter\n\n")
    assert "rejected by the schema" in provider.prompts[1]
    assert "wall_thickness" in provider.prompts[1]
    # Bounded: the whole re-ask is a fraction of the original request.
    assert len(provider.prompts[1]) < 600


def test_reask_does_not_leak_the_whole_validation_error() -> None:
    """The unbounded ``str(exc)`` must not reach the model."""
    provider = _FlakyPlannerProvider(failures=1, plan=invalid_plan_payload(error_count=6))
    node = make_planner_node(provider=provider, max_attempts=3)
    node({"prompt": "make an adapter", "skills": [], "history": []})
    try:
        PlannerPlan.model_validate(invalid_plan_payload(error_count=6))
    except ValidationError as exc:
        assert str(exc) not in provider.prompts[1]


def test_final_failure_still_reports_the_whole_error_to_the_operator() -> None:
    """Trimming the hint must not trim the operator-facing error."""
    provider = _FlakyPlannerProvider(failures=5, plan=invalid_plan_payload(error_count=6))
    node = make_planner_node(provider=provider, max_attempts=2, planner_retries=2)

    result = node({"prompt": "make an adapter", "skills": [], "history": []})

    assert result["status"] == "failed"
    error = json.loads(result["error"])
    assert error["type"] == "ValidationError"
    assert error["stage"] == "planner"
    # Every violation, not just the first three the model was shown.
    for field in ("wall_thickness", "angle", "material", "object", "mounting.count"):
        assert field in error["message"], f"the operator lost {field}"


# ---------------------------------------------------------------------------
# A response that arrived but does not fit: repairable
# ---------------------------------------------------------------------------


def _schema_mismatch_error(payload: dict[str, Any]) -> LLMResponseError:
    """Build the error a provider raises for a schema-invalid answer.

    Mirrors what :class:`~agents.llm.ollama.OllamaProvider` and
    :class:`~agents.llm.openai.OpenAICompatibleProvider` raise: the full
    ``str(exc)`` for the operator plus the Pydantic error for the hint.
    """
    try:
        PlannerPlan.model_validate(payload)
    except ValidationError as exc:
        return LLMResponseError(
            f"fake provider response does not match PlannerPlan: {exc}",
            validation_error=exc,
        )
    raise AssertionError("fixture is not invalid")


class _RaisingPlannerProvider:
    """Raises the same *error* for the first *failures* calls, then a good plan.

    Records every prompt, so ``len(provider.prompts)`` is the number of provider
    calls -- the property the "no retry" tests assert on.
    """

    name = "raising"

    def __init__(
        self,
        error: Exception,
        *,
        failures: int = 1,
        recovered: dict[str, Any] | None = None,
    ) -> None:
        self.error = error
        self.failures = failures
        self.recovered = recovered or {"specification": DEFAULT_SPEC}
        self.prompts: list[str] = []

    def supports_vision(self) -> bool:
        return False

    def generate(self, prompt: str, **kwargs: Any) -> str:
        raise AssertionError("the planner must not call generate()")

    def structured(
        self,
        prompt: str,
        schema: type[BaseModel] | dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.prompts.append(prompt)
        if len(self.prompts) <= self.failures:
            raise self.error
        return copy.deepcopy(self.recovered)


def test_provider_schema_mismatch_is_retried_with_the_bounded_complaint() -> None:
    """The measured common case: the provider rejected the model's own answer."""
    provider = _RaisingPlannerProvider(
        _schema_mismatch_error(invalid_plan_payload()),
        failures=1,
    )
    node = make_planner_node(provider=provider, max_attempts=3)

    result = node({"prompt": "make an adapter", "skills": [], "history": []})

    assert result["status"] == "planned"
    assert len(provider.prompts) == 2, "a schema mismatch must be re-asked, not failed"
    # The re-ask repeats the question *and* the concrete complaint.
    assert provider.prompts[1].startswith("make an adapter\n\n")
    assert "rejected by the schema" in provider.prompts[1]
    assert "wall_thickness" in provider.prompts[1]
    assert "greater than or equal to 0.4" in provider.prompts[1]
    # Bounded by the same cap as the ValidationError path -- no second mechanism.
    assert len(provider.prompts[1]) == len("make an adapter\n\n") + len(
        _repair_hint(_schema_mismatch_error(invalid_plan_payload()))
    )
    assert len(provider.prompts[1]) < 600


def test_provider_schema_mismatch_repair_hint_matches_the_validation_error_path() -> None:
    """Both shapes of "did not fit" must produce the very same hint."""
    payload = invalid_plan_payload(error_count=6)
    try:
        PlannerPlan.model_validate(payload)
    except ValidationError as exc:
        from_validation_error = _repair_hint(exc)
    assert _repair_hint(_schema_mismatch_error(payload)) == from_validation_error


def test_repairable_failure_without_field_detail_is_retried_bounded() -> None:
    """Invalid JSON / not-an-object: no field to name, so the whole answer is."""
    provider = _RaisingPlannerProvider(
        LLMResponseError("fake provider structured response is not valid JSON: 'nope'"),
        failures=1,
    )
    node = make_planner_node(provider=provider, max_attempts=3)

    result = node({"prompt": "make an adapter", "skills": [], "history": []})

    assert result["status"] == "planned"
    assert len(provider.prompts) == 2
    reask = provider.prompts[1]
    assert reask.startswith("make an adapter\n\n")
    assert "rejected by the schema" in reask
    assert "ONE JSON object" in reask
    # One bullet, and still inside the cap.
    assert len([line for line in reask.splitlines() if line.startswith("- ")]) == 1
    assert len(reask) <= len("make an adapter\n\n") + MAX_REPAIR_HINT_CHARS + 1


def test_repeated_repairable_failure_stops_after_attempts_and_reports_the_full_error() -> None:
    """A model that never repairs itself must still terminate, with everything."""
    provider = _RaisingPlannerProvider(
        _schema_mismatch_error(invalid_plan_payload(error_count=6)),
        failures=99,
    )
    node = make_planner_node(provider=provider, max_attempts=3, planner_retries=2)

    result = node({"prompt": "make an adapter", "skills": [], "history": []})

    assert result["status"] == "failed"
    # Exactly ``planner_retries`` calls, never more.
    assert len(provider.prompts) == 2
    error = json.loads(result["error"])
    assert error["stage"] == "planner"
    assert error["type"] == "LLMResponseError"
    # The bounded hint (3 violations) must not have become the reported error.
    for field in ("wall_thickness", "angle", "material", "object", "mounting.count"):
        assert field in error["message"], f"the operator lost {field}"
    # ...while the model only ever saw the first three.
    shown = [line for line in provider.prompts[1].splitlines() if line.startswith("- ")]
    assert len(shown) == MAX_REPAIR_ERRORS + 1  # the "+N more" line


# ---------------------------------------------------------------------------
# A request that did not complete: never retried
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            LLMError("Ollama request timed out after 120s: http://t/api/chat"), id="timeout"
        ),
        pytest.param(LLMError("Ollama HTTP 500 on http://t/api/chat: boom"), id="http-error"),
        pytest.param(LLMError("Ollama returned an empty generate response"), id="empty-response"),
    ],
)
def test_transport_failure_is_never_retried(error: LLMError) -> None:
    """Counting provider calls, because this is what regresses silently."""
    provider = _RaisingPlannerProvider(error, failures=99)
    node = make_planner_node(provider=provider, max_attempts=3, planner_retries=2)

    result = node({"prompt": "make an adapter", "skills": [], "history": []})

    assert result["status"] == "failed"
    assert len(provider.prompts) == 1, "a request that never completed must not be re-asked"
    error_json = json.loads(result["error"])
    assert error_json["type"] == "LLMError"
    assert error_json["message"] == str(error)


# ---------------------------------------------------------------------------
# The same two properties through the real providers (stubbed transport)
# ---------------------------------------------------------------------------


def _ollama_answering(monkeypatch, *results: Any) -> tuple[OllamaProvider, list[Any]]:
    """A real ``OllamaProvider`` replaying canned ``/api/chat`` results."""
    queue = list(results)
    calls: list[Any] = []

    def replay(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((path, payload))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    provider = OllamaProvider(
        base_url="http://test:11434",
        model="test-model",
        get_setting=lambda name: None,
    )
    monkeypatch.setattr(provider, "_post", replay)
    return provider, calls


def _ollama_socket_failing(monkeypatch, error: Exception) -> tuple[OllamaProvider, list[Any]]:
    """A real ``OllamaProvider`` whose socket fails; counts every attempt."""
    calls: list[Any] = []

    def boom(*args: Any, **kwargs: Any) -> None:
        calls.append(kwargs)
        raise error

    monkeypatch.setattr("agents.llm.ollama.urllib.request.urlopen", boom)
    provider = OllamaProvider(
        base_url="http://test:11434",
        model="test-model",
        get_setting=lambda name: None,
    )
    return provider, calls


class _SdkMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _SdkChoice:
    def __init__(self, content: str | None) -> None:
        self.message = _SdkMessage(content)


class _SdkResponse:
    def __init__(self, content: str | None) -> None:
        self.choices = [_SdkChoice(content)]


class _SdkCompletions:
    def __init__(self, queue: list[Any], calls: list[Any]) -> None:
        self.queue = queue
        self.calls = calls

    def create(self, **request: Any) -> Any:
        self.calls.append(request)
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _SdkClient:
    """Stand-in for ``openai.OpenAI`` exposing only ``chat.completions``."""

    def __init__(self, *responses: Any) -> None:
        self.calls: list[Any] = []
        self.chat = self
        self.completions = _SdkCompletions(list(responses), self.calls)


def _openai_answering(*responses: Any) -> tuple[OpenAICompatibleProvider, list[Any]]:
    client = _SdkClient(*responses)
    provider = OpenAICompatibleProvider(
        client=client,
        base_url="http://gateway:8000/v1",
        api_key="sk-test",
        model="test-model",
        get_setting=lambda name: None,
    )
    return provider, client.calls


def _chat(content: str) -> dict[str, Any]:
    return {"message": {"content": content}}


BAD_PLAN = json.dumps(invalid_plan_payload())
GOOD_PLAN = json.dumps({"specification": DEFAULT_SPEC})


def _reask_prompt(calls: list[Any], backend: str) -> str:
    """The user message of the second provider call, for either backend."""
    call = calls[1]
    payload = call[1] if backend == "ollama" else call
    return payload["messages"][-1]["content"]


@pytest.mark.parametrize("backend", ["ollama", "openai"])
def test_real_provider_schema_mismatch_is_retried(monkeypatch, backend: str) -> None:
    """End to end: a real backend's own schema rejection re-asks the model."""
    if backend == "ollama":
        provider, calls = _ollama_answering(monkeypatch, _chat(BAD_PLAN), _chat(GOOD_PLAN))
    else:
        provider, calls = _openai_answering(_SdkResponse(BAD_PLAN), _SdkResponse(GOOD_PLAN))
    node = make_planner_node(provider=provider, max_attempts=3)

    result = node({"prompt": "make an adapter", "skills": [], "history": []})

    assert result["status"] == "planned"
    assert len(calls) == 2
    reask = _reask_prompt(calls, backend)
    assert "rejected by the schema" in reask
    assert "wall_thickness" in reask
    assert len(reask) < 600


@pytest.mark.parametrize("backend", ["ollama", "openai"])
def test_real_provider_timeout_is_not_retried(monkeypatch, backend: str) -> None:
    """End to end: a real backend's timeout costs exactly one round trip."""
    if backend == "ollama":
        provider, calls = _ollama_socket_failing(monkeypatch, TimeoutError("timed out"))
    else:
        provider, calls = _openai_answering(TimeoutError("timed out"))
    node = make_planner_node(provider=provider, max_attempts=3, planner_retries=2)

    result = node({"prompt": "make an adapter", "skills": [], "history": []})

    assert result["status"] == "failed"
    assert len(calls) == 1, "a timeout must not be re-asked"
    assert json.loads(result["error"])["type"] == "LLMError"


@pytest.mark.parametrize("backend", ["ollama", "openai"])
def test_real_provider_http_error_is_not_retried(monkeypatch, backend: str) -> None:
    if backend == "ollama":
        provider, calls = _ollama_socket_failing(
            monkeypatch,
            urllib.error.HTTPError("http://t/api/chat", 500, "boom", {}, io.BytesIO(b"e")),
        )
    else:
        provider, calls = _openai_answering(RuntimeError("Error code: 500 - server error"))
    node = make_planner_node(provider=provider, max_attempts=3, planner_retries=2)

    result = node({"prompt": "make an adapter", "skills": [], "history": []})

    assert result["status"] == "failed"
    assert len(calls) == 1, "an HTTP error must not be re-asked"
    assert json.loads(result["error"])["type"] == "LLMError"
