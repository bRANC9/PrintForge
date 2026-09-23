"""Planner clarification + assumption behaviour (docs/planner-clarification.md).

No database, no network: the graph dependencies are fakes injected through
:class:`~agents.graph.WorkflowDeps`. These tests pin the routing contract --
``ask`` stops the run before any specification/CAD, ``assume`` continues and
records the guesses -- plus the ``max_length=8`` bound and the Editor parity.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from agents.graph import WorkflowDeps, run_workflow
from agents.graph.planner import PlannerPlan
from agents.graph.tests.fakes import DEFAULT_SPEC, FakeCADBackend, FakeProvider
from agents.graph.workflow import route_after_planner
from agents.spec import ModelSpecification

ASSUMED = {
    "question": "No wall thickness given; assuming a printable 4 mm wall.",
    "answer": "4",
    "kind": "assumed",
    "field": "wall_thickness",
}
BLOCKING = {
    "question": "Which phone model is the holder for?",
    "answer": "",
    "kind": "needs_user_input",
    "field": "dimensions",
}
ANNOTATION = {
    "id": "c1",
    "kind": "point",
    "point": [35.0, 12.5, 8.0],
    "normal": [0.0, 0.0, 1.0],
    "faces": [],
    "instruction": "4 mm-es átmenő lyuk",
}


def _never_retrieve(*args: Any, **kwargs: Any) -> list[dict]:
    raise AssertionError("retrieve() must not be called when research is not needed")


def _deps(provider: FakeProvider, cad: FakeCADBackend, **kwargs: Any) -> WorkflowDeps:
    kwargs.setdefault("retrieve_fn", _never_retrieve)
    kwargs.setdefault("max_attempts", 3)
    return WorkflowDeps(provider=provider, cad_backend=cad, **kwargs)


def _plan(*clarifications: dict[str, Any]) -> dict[str, Any]:
    return {
        "specification": DEFAULT_SPEC,
        "needs_research": False,
        "research_query": None,
        "clarifications": list(clarifications),
    }


# ---------------------------------------------------------------------------
# PlannerPlan contract
# ---------------------------------------------------------------------------


def test_planner_plan_carries_clarifications():
    plan = PlannerPlan.model_validate(_plan(ASSUMED, BLOCKING))

    assert [item.kind for item in plan.clarifications] == ["assumed", "needs_user_input"]
    assert plan.clarifications[0].answer == "4"
    assert plan.clarifications[1].answer == ""


def test_planner_plan_rejects_more_than_eight_clarifications():
    with pytest.raises(ValidationError):
        PlannerPlan.model_validate(_plan(*([ASSUMED] * 9)))


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_route_after_planner_ends_on_clarification_and_failure():
    assert route_after_planner({"status": "clarification"}) == "__end__"
    assert route_after_planner({"status": "failed"}) == "__end__"
    assert route_after_planner({"status": "planned", "needs_research": True}) == "research"
    assert route_after_planner({"status": "planned", "needs_research": False}) == "cad"


# ---------------------------------------------------------------------------
# assume policy (the default)
# ---------------------------------------------------------------------------


def test_assumed_defaults_continue_and_are_recorded():
    provider = FakeProvider(plan=_plan(ASSUMED))
    cad = FakeCADBackend()

    state = run_workflow("make a holder", deps=_deps(provider, cad))

    assert state["status"] == "done"
    assert state["assumptions"] == [
        {
            "question": ASSUMED["question"],
            "answer": "4",
            "kind": "assumed",
            "field": "wall_thickness",
        }
    ]
    assert state["clarifications"] == []
    assert any("review required" in entry for entry in state["history"])
    # The specification was finalised and reached the CAD agent.
    assert state["specification"] == DEFAULT_SPEC
    assert cad.generate_calls == 1


def test_blocking_question_under_assume_policy_does_not_stop_the_run():
    provider = FakeProvider(plan=_plan(BLOCKING))
    cad = FakeCADBackend()

    state = run_workflow("make a holder", deps=_deps(provider, cad), clarify_policy="assume")

    assert state["status"] == "done"
    assert state["clarifications"] == []
    assert state["assumptions"] == []
    assert cad.generate_calls == 1


# ---------------------------------------------------------------------------
# ask policy
# ---------------------------------------------------------------------------


def test_blocking_question_under_ask_policy_stops_before_cad():
    provider = FakeProvider(plan=_plan(BLOCKING))
    cad = FakeCADBackend()

    state = run_workflow("make a holder", deps=_deps(provider, cad), clarify_policy="ask")

    assert state["status"] == "clarification"
    assert state["clarifications"] == [
        {
            "question": BLOCKING["question"],
            "answer": "",
            "kind": "needs_user_input",
            "field": "dimensions",
        }
    ]
    assert state["assumptions"] == []
    # No specification was finalised and no CAD/mesh work happened.
    assert "specification" not in state
    assert "scad_source" not in state
    assert cad.generate_calls == 0
    assert cad.validate_calls == 0
    assert cad.export_calls == 0
    assert any("question(s) for the user" in entry for entry in state["history"])


def test_ask_policy_splits_assumed_from_blocking():
    provider = FakeProvider(plan=_plan(ASSUMED, BLOCKING))
    cad = FakeCADBackend()

    state = run_workflow("make a holder", deps=_deps(provider, cad), clarify_policy="ask")

    assert state["status"] == "clarification"
    # Only the blocking question stops the run ...
    assert [item["field"] for item in state["clarifications"]] == ["dimensions"]
    # ... the safe assumption is still recorded.
    assert [item["field"] for item in state["assumptions"]] == ["wall_thickness"]
    assert cad.generate_calls == 0


def test_no_blocking_question_keeps_planned_status_under_ask_policy():
    provider = FakeProvider(plan=_plan(ASSUMED))
    cad = FakeCADBackend()

    state = run_workflow("make a holder", deps=_deps(provider, cad), clarify_policy="ask")

    assert state["status"] == "done"
    assert state["assumptions"]


# ---------------------------------------------------------------------------
# Editor parity (docs/visual-editing.md 3.5)
# ---------------------------------------------------------------------------


def test_editor_can_also_stop_for_clarification():
    provider = FakeProvider(plan=_plan(BLOCKING))
    cad = FakeCADBackend()

    state = run_workflow(
        "A kijelölt peremre tegyél egy lyukat.",
        deps=_deps(provider, cad),
        base_specification=ModelSpecification.example(),
        annotations=[ANNOTATION],
        clarify_policy="ask",
    )

    assert state["edit_mode"] is True
    assert state["status"] == "clarification"
    assert state["clarifications"][0]["field"] == "dimensions"
    assert cad.generate_calls == 0
    assert any(entry.startswith("editor:") for entry in state["history"])


def test_editor_assumption_continues_with_edit_specification():
    planned = {
        "specification": DEFAULT_SPEC,
        "needs_research": False,
        "research_query": None,
        "clarifications": [ASSUMED],
    }
    provider = FakeProvider(plan=planned)
    cad = FakeCADBackend()

    state = run_workflow(
        "A kijelölt peremre tegyél egy lyukat.",
        deps=_deps(provider, cad),
        base_specification=ModelSpecification.example(),
        annotations=[ANNOTATION],
        clarify_policy="ask",
    )

    assert state["status"] == "done"
    assert state["assumptions"]
    assert any("editor:" in entry and "review required" in entry for entry in state["history"])
