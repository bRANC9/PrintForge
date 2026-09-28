"""Planner node tests: the bounded schema-repair re-ask.

No network, no database, no OpenSCAD: a fake provider returns an invalid plan
once and then a valid one, and the test inspects what the second call was told.
"""

from __future__ import annotations

import copy
import json
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
