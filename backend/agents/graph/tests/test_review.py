"""Unit + workflow tests for the vision self-check (docs/vision-self-check.md).

No network, no database, no OpenSCAD and no real mesh rendering: the provider,
the preview renderer and the CAD backend are all fakes.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END

from agents.graph import WorkflowDeps, run_workflow
from agents.graph.review import REVIEW_SYSTEM_PROMPT, make_review_node
from agents.graph.tests.fakes import (
    DEFAULT_SPEC,
    FakeCADBackend,
    FakeProvider,
    FakeVisionProvider,
)
from agents.graph.workflow import route_after_review, route_after_validate
from agents.llm import LLMError
from designs.cad.preview import PreviewRenderError

PREVIEW_PNG = b"\x89PNG\r\n\x1a\nfake-preview"


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "prompt": "make a 70 mm phone holder",
        "specification": DEFAULT_SPEC,
        "stl_bytes": b"solid fake",
        "attempt": 1,
        "max_attempts": 3,
        "status": "done",
        "validation": {"status": "valid", "attempt": 1, "max_attempts": 3, "errors": []},
        "history": ["validator: valid (attempt 1)"],
    }
    state.update(overrides)
    return state


def _deps(provider: Any, cad: Any, **kwargs: Any) -> WorkflowDeps:
    kwargs.setdefault("retrieve_fn", lambda *args, **k: [])
    kwargs.setdefault("max_attempts", 3)
    kwargs.setdefault("preview_renderer", lambda _stl: PREVIEW_PNG)
    return WorkflowDeps(provider=provider, cad_backend=cad, **kwargs)


# ---------------------------------------------------------------------------
# Node-level behaviour
# ---------------------------------------------------------------------------


def test_review_skips_when_there_is_no_stl():
    def boom(_stl: bytes) -> bytes:
        raise AssertionError("must not render without an STL")

    node = make_review_node(FakeVisionProvider(), render_preview=boom)

    result = node(_state(stl_bytes=b""))

    assert result["vision_review"] == {"skipped": True, "reason": "no_stl"}
    assert result["vision_used"] is False
    assert "status" not in result  # status is preserved
    assert result["history"][-1].startswith("review:")


def test_no_vision_skips_without_rendering():
    rendered: list[bytes] = []

    def render(stl: bytes) -> bytes:
        rendered.append(stl)
        raise AssertionError("a text-only provider must not render a preview")

    node = make_review_node(FakeProvider(), render_preview=render)  # supports_vision=False

    result = node(_state())

    assert result["vision_review"] == {"skipped": True, "reason": "no_vision"}
    assert result["vision_used"] is False
    assert "status" not in result  # the run stays "done"
    assert rendered == []


def test_mismatch_triggers_a_retry_while_attempts_remain():
    provider = FakeVisionProvider(
        reviews=[{"matches": False, "issues": ["the body is too short"], "summary": "mismatch"}]
    )
    node = make_review_node(provider, render_preview=lambda _stl: PREVIEW_PNG)

    result = node(_state(attempt=1, max_attempts=3))

    assert result["status"] == "retry"
    assert result["vision_used"] is True
    assert result["preview_image"] == PREVIEW_PNG
    assert result["vision_review"]["matches"] is False
    # The issues reach the CAD node's reviser through the validation errors.
    assert result["validation"]["errors"] == ["the body is too short"]

    # The reviewer received the rendered image, the system prompt and a prompt
    # that carries the original request and the specification.
    assert provider.review_images == [[PREVIEW_PNG]]
    assert provider.review_systems == [REVIEW_SYSTEM_PROMPT]
    prompt = provider.review_prompts[0]
    assert "make a 70 mm phone holder" in prompt
    assert DEFAULT_SPEC["object"] in prompt


def test_mismatch_stops_retrying_when_attempts_are_exhausted():
    provider = FakeVisionProvider(
        reviews=[
            {
                "matches": False,
                "issues": ["wrong width", "missing hole"],
                "summary": "still off",
            }
        ]
    )
    node = make_review_node(provider, render_preview=lambda _stl: PREVIEW_PNG)

    result = node(_state(attempt=3, max_attempts=3))

    assert result["status"] == "done"  # a valid model is never blocked
    assert result["vision_used"] is True
    assert result["vision_review"]["matches"] is False
    assert "validation" not in result
    warning = result["history"][-1]
    assert warning.startswith("review: mismatch after 3 attempt(s)")
    assert "wrong width" in warning
    assert "missing hole" in warning


def test_match_keeps_done_and_records_the_preview():
    provider = FakeVisionProvider(reviews=[{"matches": True, "issues": [], "summary": "ok"}])
    node = make_review_node(provider, render_preview=lambda _stl: PREVIEW_PNG)

    result = node(_state())

    assert result["status"] == "done"
    assert result["vision_used"] is True
    assert result["preview_image"] == PREVIEW_PNG
    assert result["vision_review"] == {"matches": True, "issues": [], "summary": "ok"}


def test_review_prompt_carries_the_reference_note():
    provider = FakeVisionProvider(reviews=[{"matches": True, "summary": "ok"}])
    node = make_review_node(provider, render_preview=lambda _stl: PREVIEW_PNG)

    node(_state(reference_image_note="70 mm wide"))

    assert "70 mm wide" in provider.review_prompts[0]


def test_render_failure_is_swallowed_into_a_warning():
    def boom(_stl: bytes) -> bytes:
        raise PreviewRenderError("degenerate mesh")

    provider = FakeVisionProvider()
    node = make_review_node(provider, render_preview=boom)

    result = node(_state())

    assert result["vision_review"] == {"skipped": True, "reason": "render_failed"}
    assert result["vision_used"] is False
    assert "render failed" in result["history"][-1]
    assert provider.calls == []  # no LLM call after a failed render


def test_unexpected_render_exception_is_also_swallowed():
    def boom(_stl: bytes) -> bytes:
        raise RuntimeError("pillow exploded")

    node = make_review_node(FakeVisionProvider(), render_preview=boom)

    result = node(_state())

    assert result["vision_review"] == {"skipped": True, "reason": "render_failed"}
    assert result["vision_used"] is False


def test_vision_llm_error_is_swallowed_into_a_warning():
    class OfflineProvider(FakeVisionProvider):
        def structured(self, prompt: str, schema: Any, **kwargs: Any) -> dict[str, Any]:
            if getattr(schema, "__name__", "") == "ReviewResult":
                raise LLMError("vision model offline")
            return super().structured(prompt, schema, **kwargs)

    node = make_review_node(OfflineProvider(), render_preview=lambda _stl: PREVIEW_PNG)

    result = node(_state())

    assert result["vision_review"] == {"skipped": True, "reason": "llm_error"}
    assert result["vision_used"] is False
    assert "vision call failed" in result["history"][-1]


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_route_after_validate_sends_valid_models_to_review():
    assert route_after_validate({"status": "done"}) == "review"
    assert route_after_validate({"status": "retry"}) == "cad"
    assert route_after_validate({"status": "failed"}) == END


def test_route_after_review_only_retries_on_a_retry_status():
    assert route_after_review({"status": "retry"}) == "cad"
    assert route_after_review({"status": "done"}) == END
    assert route_after_review({}) == END


# ---------------------------------------------------------------------------
# Workflow-level integration
# ---------------------------------------------------------------------------


def test_valid_model_routes_through_review_and_back_to_cad_once():
    provider = FakeVisionProvider(
        reviews=[
            {"matches": False, "issues": ["too short"], "summary": "mismatch"},
            {"matches": True, "issues": [], "summary": "ok"},
        ]
    )
    cad = FakeCADBackend()

    state = run_workflow("make a phone holder", deps=_deps(provider, cad))

    assert state["status"] == "done"
    assert state["attempt"] == 2  # exactly one review-driven retry
    assert cad.generate_calls == 2
    assert cad.validate_calls == 2
    assert cad.export_calls == 2
    assert provider.calls == ["PlannerPlan", "ReviewResult", "ReviewResult"]
    assert state["vision_used"] is True
    assert state["vision_review"]["matches"] is True
    assert state["preview_image"] == PREVIEW_PNG
    assert any("review: mismatch" in entry for entry in state["history"])


def test_workflow_without_vision_still_finishes_done():
    provider = FakeProvider()  # supports_vision() is False
    cad = FakeCADBackend()

    state = run_workflow("make a phone holder", deps=_deps(provider, cad))

    assert state["status"] == "done"
    assert state["vision_used"] is False
    assert state["vision_review"] == {"skipped": True, "reason": "no_vision"}
    assert provider.calls == ["PlannerPlan"]  # no vision call was made


def test_review_runs_on_the_dedicated_vision_provider():
    """A text-only main provider plus a vision ``review_provider`` still reviews.

    The review node is built with the dedicated provider, so the self-check runs
    even though the planner/editor provider has no vision -- and the vision
    provider is the one that receives the rendered preview image.
    """
    main = FakeProvider()  # supports_vision() is False
    reviewer = FakeVisionProvider(reviews=[{"matches": True, "issues": [], "summary": "ok"}])
    cad = FakeCADBackend()

    state = run_workflow(
        "make a phone holder",
        deps=_deps(main, cad, review_provider=reviewer),
    )

    assert state["status"] == "done"
    assert state["vision_used"] is True
    assert state["vision_review"] == {"matches": True, "issues": [], "summary": "ok"}
    assert state["preview_image"] == PREVIEW_PNG
    # The dedicated vision provider judged the rendered preview ...
    assert reviewer.review_images == [[PREVIEW_PNG]]
    assert reviewer.calls == ["ReviewResult"]
    # ... while the main (non-vision) provider was only used by the Planner.
    assert main.calls == ["PlannerPlan"]


def test_review_falls_back_to_the_main_provider_without_a_review_provider():
    """With ``review_provider=None`` the review keeps using the main provider."""
    provider = FakeVisionProvider(reviews=[{"matches": True, "issues": [], "summary": "ok"}])
    cad = FakeCADBackend()

    state = run_workflow("make a phone holder", deps=_deps(provider, cad))

    assert state["vision_used"] is True
    assert provider.calls == ["PlannerPlan", "ReviewResult"]
