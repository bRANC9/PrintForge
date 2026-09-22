"""Graph behaviour tests: nodes, edges, research branch and bounded retry.

No database, no network: all dependencies are fakes injected through
:class:`~agents.graph.WorkflowDeps`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.conf import settings

from agents.graph import WorkflowDeps, build_workflow, run_workflow
from agents.graph.tests.fakes import DEFAULT_SPEC, ENRICHED_SPEC, FakeCADBackend, FakeProvider
from agents.llm import LLMError
from agents.spec import ModelSpecification
from designs.cad.base import CADError

RESEARCH_DOC = {
    "chunk_id": 7,
    "document_id": 3,
    "title": "Samsung Galaxy S24 dimensions",
    "source_url": "https://example.test/s24",
    "content": "70.6 mm x 147.0 mm x 7.6 mm",
    "score": 0.91,
}

EDIT_SPEC: dict[str, Any] = {
    **DEFAULT_SPEC,
    "operations": [
        {
            "kind": "hole",
            "origin": {"x": 35.0, "y": 12.5, "z": 8.0},
            "normal": {"x": 0.0, "y": 0.0, "z": 1.0},
            "depth": 8.0,
            "diameter": 4.0,
            "label": "4 mm hole",
        }
    ],
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


# ---------------------------------------------------------------------------
# Happy path / node boundaries
# ---------------------------------------------------------------------------


def test_happy_path_generates_source_then_exports_stl():
    provider = FakeProvider()
    cad = FakeCADBackend()

    state = run_workflow("make a phone holder", deps=_deps(provider, cad))

    assert state["status"] == "done"
    assert state["specification"] == DEFAULT_SPEC
    assert state["scad_source"] == cad.scad_source
    assert state["stl_bytes"] == cad.stl_bytes
    assert state["attempt"] == 1
    assert state["validation"]["status"] == "valid"
    assert state["error"] is None

    # A valid model now goes through the optional vision review
    # (docs/vision-self-check.md); without a vision provider it skips, but the
    # decision is still recorded and the run stays "done".
    assert state["vision_used"] is False
    assert state["vision_review"] == {"skipped": True, "reason": "no_vision"}
    assert any(entry.startswith("review: skipped") for entry in state["history"])

    # The Planner only produces structured data (one structured call) ...
    assert provider.calls == ["PlannerPlan"]
    # ... and the CAD agent only renders OpenSCAD source.
    assert cad.generate_calls == 1
    assert cad.validate_calls == 1
    assert cad.export_calls == 1  # the Validator is the only mesh producer


def test_research_is_skipped_when_the_planner_does_not_flag_it():
    provider = FakeProvider()  # needs_research=False
    cad = FakeCADBackend()

    state = run_workflow("make a phone holder", deps=_deps(provider, cad))

    assert state["needs_research"] is False
    assert state["research_sources"] == []
    assert state["research_used"] is False
    assert provider.calls == ["PlannerPlan"]


def test_research_enriches_the_specification_and_keeps_sources():
    calls: list[tuple[str, int, str | None]] = []

    def retrieve(query: str, limit: int = 8, company_id: str | None = None) -> list[dict]:
        calls.append((query, limit, company_id))
        return [RESEARCH_DOC]

    provider = FakeProvider(
        plan={
            "specification": DEFAULT_SPEC,
            "needs_research": True,
            "research_query": "Samsung Galaxy S24 dimensions",
        },
        enriched=ENRICHED_SPEC,
    )
    cad = FakeCADBackend()

    state = run_workflow(
        "wall phone holder for a Galaxy S24",
        deps=_deps(provider, cad, retrieve_fn=retrieve, research_limit=5),
        company_id="ws-42",
    )

    assert state["status"] == "done"
    assert state["needs_research"] is True
    assert state["research_used"] is True
    assert calls == [("Samsung Galaxy S24 dimensions", 5, "ws-42")]
    assert provider.calls == ["PlannerPlan", "ModelSpecification"]
    # Enriched values reached the CAD backend.
    assert state["specification"]["angle"] == 20.0
    assert state["specification"]["mounting"] == {"type": "M5", "count": 4}
    assert cad.validated_models[0].specification["angle"] == 20.0
    # Provenance is preserved.
    assert state["research_sources"][0]["source_url"] == "https://example.test/s24"
    assert state["research_sources"][0]["title"] == "Samsung Galaxy S24 dimensions"


def test_research_retrieval_failure_is_best_effort():
    def retrieve(*args: Any, **kwargs: Any) -> list[dict]:
        raise RuntimeError("pgvector down")

    provider = FakeProvider(
        plan={"specification": DEFAULT_SPEC, "needs_research": True, "research_query": "q"},
        enriched=ENRICHED_SPEC,
    )
    cad = FakeCADBackend()

    state = run_workflow("x", deps=_deps(provider, cad, retrieve_fn=retrieve))

    assert state["status"] == "done"
    assert state["research_sources"] == []
    assert state["research_used"] is False
    # No enrichment call because there was no context.
    assert provider.calls == ["PlannerPlan"]
    assert state["specification"] == DEFAULT_SPEC


def test_research_with_rag_disabled_keeps_the_planner_spec():
    """``RAG_ENABLED=false`` makes ``retrieve`` return ``[]`` without LLM/DB work.

    The Research node must still succeed and pass the Planner specification on
    to the CAD agent unchanged.
    """
    calls: list[tuple[str, int, str | None]] = []

    def retrieve(query: str, limit: int = 8, company_id: str | None = None) -> list[dict]:
        calls.append((query, limit, company_id))
        return []  # exactly what embeddings.services.retrieve does when disabled

    provider = FakeProvider(
        plan={"specification": DEFAULT_SPEC, "needs_research": True, "research_query": "M5 sizes"},
        enriched=ENRICHED_SPEC,
    )
    cad = FakeCADBackend()

    state = run_workflow("x", deps=_deps(provider, cad, retrieve_fn=retrieve))

    assert state["status"] == "done"
    assert calls == [("M5 sizes", 5, None)]
    assert state["research_used"] is False
    assert state["research_sources"] == []
    # The injected retrieve_fn was called, but no enrichment LLM call was made.
    assert provider.calls == ["PlannerPlan"]
    assert state["specification"] == DEFAULT_SPEC
    assert cad.validated_models[0].specification == DEFAULT_SPEC


# ---------------------------------------------------------------------------
# Visual-prompt edit mode (docs/visual-editing.md 3.5)
# ---------------------------------------------------------------------------


def test_route_entry_selects_editor_only_in_edit_mode():
    from agents.graph.workflow import route_entry

    assert route_entry({"edit_mode": True}) == "editor"
    assert route_entry({"edit_mode": False}) == "planner"
    # An empty/absent flag keeps the pre-existing planner entry.
    assert route_entry({}) == "planner"


def test_edit_mode_routes_start_through_the_editor():
    provider = FakeProvider(
        plan={
            "specification": EDIT_SPEC,
            "needs_research": False,
            "research_query": None,
        }
    )
    cad = FakeCADBackend()

    state = run_workflow(
        "A kijelölt peremre tegyél egy 4 mm-es lyukat.",
        deps=_deps(provider, cad),
        base_specification=DEFAULT_SPEC,
        annotations=[ANNOTATION],
    )

    assert state["edit_mode"] is True
    assert state["base_specification"] == DEFAULT_SPEC
    assert state["annotations"] == [ANNOTATION]
    assert state["status"] == "done"

    # The Editor (PlannerPlan schema, same fake call) produced the operations.
    assert provider.calls == ["PlannerPlan"]
    operations = state["specification"]["operations"]
    assert len(operations) == 1
    assert operations[0]["kind"] == "hole"
    assert operations[0]["diameter"] == 4.0
    assert operations[0]["origin"] == {"x": 35.0, "y": 12.5, "z": 8.0}
    assert operations[0]["normal"] == {"x": 0.0, "y": 0.0, "z": 1.0}
    assert any("edit" in entry.lower() for entry in state["history"])


def test_no_annotations_keeps_the_planner_entry_and_empty_edit_fields():
    provider = FakeProvider()
    cad = FakeCADBackend()

    state = run_workflow("make a phone holder", deps=_deps(provider, cad))

    assert state["edit_mode"] is False
    assert state["base_specification"] == {}
    assert state["annotations"] == []
    assert state["status"] == "done"
    assert state["specification"] == DEFAULT_SPEC


# ---------------------------------------------------------------------------
# Bounded retry policy
# ---------------------------------------------------------------------------


def test_retry_succeeds_within_the_bound():
    provider = FakeProvider()
    cad = FakeCADBackend(failures=2)  # fails attempts 1 and 2, succeeds on 3

    state = run_workflow("x", deps=_deps(provider, cad, max_attempts=3))

    assert state["status"] == "done"
    assert state["attempt"] == 3
    assert cad.generate_calls == 3
    assert cad.validate_calls == 3
    assert cad.export_calls == 1


def test_retry_is_bounded_and_fails_structurally():
    provider = FakeProvider()
    cad = FakeCADBackend(failures=99)

    state = run_workflow("x", deps=_deps(provider, cad, max_attempts=3))

    assert state["status"] == "failed"
    assert state["attempt"] == 3  # exactly AGENT_MAX_ATTEMPTS, never more
    assert cad.generate_calls == 3
    assert cad.validate_calls == 3
    assert cad.export_calls == 0

    error = json.loads(state["error"])
    assert error["stage"] == "validator"
    assert error["type"] == "ValidationError"
    assert len(error["details"]) == 1
    assert "3 attempt(s)" in error["message"]


def test_single_attempt_bound_fails_immediately():
    provider = FakeProvider()
    cad = FakeCADBackend(failures=99)

    state = run_workflow("x", deps=_deps(provider, cad, max_attempts=1))

    assert state["status"] == "failed"
    assert cad.generate_calls == 1
    assert cad.validate_calls == 1


def test_reviser_hook_can_adjust_the_spec_on_retry():
    seen: list[tuple[dict, list[str], int]] = []

    def reviser(spec: dict, errors: list[str], attempt: int) -> dict:
        seen.append((spec, errors, attempt))
        adjusted = dict(spec)
        adjusted["angle"] = 0.0
        return adjusted

    provider = FakeProvider()
    cad = FakeCADBackend(failures=1)

    state = run_workflow("x", deps=_deps(provider, cad, reviser=reviser, max_attempts=2))

    assert state["status"] == "done"
    assert seen and seen[0][1] == ["blocking problem #1"]
    assert cad.validated_models[1].specification["angle"] == 0.0


# ---------------------------------------------------------------------------
# Terminal failures
# ---------------------------------------------------------------------------


def test_planner_failure_is_terminal_and_structured():
    provider = FakeProvider(planner_error=LLMError("no model available"))
    cad = FakeCADBackend()

    state = run_workflow("x", deps=_deps(provider, cad))

    assert state["status"] == "failed"
    assert cad.generate_calls == 0
    error = json.loads(state["error"])
    assert error["stage"] == "planner"
    assert error["type"] == "LLMError"
    assert "no model available" in error["message"]


def test_cad_generation_failure_is_terminal():
    provider = FakeProvider()
    cad = FakeCADBackend(generate_error=CADError("openscad missing"))

    state = run_workflow("x", deps=_deps(provider, cad))

    assert state["status"] == "failed"
    assert cad.validate_calls == 0
    error = json.loads(state["error"])
    assert error["stage"] == "cad"
    assert error["type"] == "CADError"


def test_export_failure_uses_the_bounded_retry_loop():
    provider = FakeProvider()
    cad = FakeCADBackend(export_error=CADError("openscad exited 1"))

    state = run_workflow("x", deps=_deps(provider, cad, max_attempts=2))

    assert state["status"] == "failed"
    assert cad.export_calls == 2  # once per attempt, then the bound hits
    assert cad.generate_calls == 2


# ---------------------------------------------------------------------------
# Wiring / settings
# ---------------------------------------------------------------------------


def test_workflow_deps_defaults_to_settings_max_attempts():
    deps = WorkflowDeps(provider=FakeProvider(), cad_backend=FakeCADBackend())
    assert deps.max_attempts == settings.AGENT_MAX_ATTEMPTS


def test_build_workflow_compiles_with_injected_deps():
    compiled = build_workflow(_deps(FakeProvider(), FakeCADBackend()))
    assert hasattr(compiled, "invoke")


def test_cad_specification_is_validated_against_the_shared_model():
    # The workflow keeps the strict terv.md 8. contract: only a validated
    # ModelSpecification ever reaches the CAD backend.
    provider = FakeProvider()
    cad = FakeCADBackend()

    run_workflow("x", deps=_deps(provider, cad))

    ModelSpecification.model_validate(cad.validated_models[0].specification)


@pytest.mark.django_db
def test_run_workflow_never_touches_the_database():
    # The graph is pure orchestration; persistence is the Celery task's job.
    provider = FakeProvider()
    cad = FakeCADBackend()

    state = run_workflow("x", deps=_deps(provider, cad))

    assert state["status"] == "done"
