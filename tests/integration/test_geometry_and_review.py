"""Prompt-dependent geometry + vision self-check (docs/cad-primitives.md,
docs/vision-self-check.md).

Integration coverage for the *task/API* seams of the two new features. The
graph-level behaviour is already pinned by ``backend/agents/graph/tests``
(``test_review.py``/``test_workflow.py``/``test_tasks.py``); this module adds the
end-to-end requests and the Celery task body, reusing the deterministic fakes
from ``agents.graph.tests.fakes`` so there is **no live LLM, Ollama, OpenSCAD,
Redis or real mesh rendering** anywhere.

Overlap note: the review node's own routing/skip-render behaviour is covered by
``agents/graph/tests/test_review.py`` (e.g.
``test_no_vision_skips_without_rendering``). Here we assert the same guarantee
at the integration level (a persisted ``ModelVersion`` is produced, no retry and
no preview) rather than re-testing the node in isolation.
"""

from __future__ import annotations

from typing import Any

import pytest
from factories import ProjectFactory

from agents.graph import WorkflowDeps
from agents.graph.tests.fakes import FakeCADBackend, FakeProvider, FakeVisionProvider
from agents.models import AgentRun, AgentRunStatus
from agents.spec import ModelSpecification
from agents.tasks import run_agent_workflow
from designs.cad.preview import PreviewRenderError
from files.services import LocalStorage

pytestmark = pytest.mark.django_db

PROMPT = "make a 40 x 20 x 10 mm bracket with a 5 mm hole"
PREVIEW_PNG = b"\x89PNG\r\n\x1a\nfake-preview"


# ---------------------------------------------------------------------------
# 1. Preview artifact endpoint
# ---------------------------------------------------------------------------


def test_preview_artifact_is_served_inline(monkeypatch, tmp_path, version, auth_client):
    """A version with a stored preview streams it as an inline PNG."""
    storage = LocalStorage(root=tmp_path)
    monkeypatch.setattr("designs.services.get_storage", lambda: storage)
    relative = f"projects/{version.project_id}/v{version.version}/preview.png"
    storage.write_bytes(relative, PREVIEW_PNG)
    version.preview_image.name = relative
    version.save(update_fields=["preview_image"])

    response = auth_client.get(f"/api/v1/versions/{version.id}/artifact/preview/")

    assert response.status_code == 200, response.content
    assert response["Content-Type"] == "image/png"
    assert response["Content-Disposition"].startswith("inline")
    assert response.content == PREVIEW_PNG


def test_missing_preview_artifact_returns_404(monkeypatch, tmp_path, version, auth_client):
    """A version without a preview (or a missing file) 404s -- like stl/scad."""
    monkeypatch.setattr("designs.services.get_storage", lambda: LocalStorage(root=tmp_path))

    response = auth_client.get(f"/api/v1/versions/{version.id}/artifact/preview/")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 2. Primitives persistence through the agent task
# ---------------------------------------------------------------------------

BOX_PRIMITIVE: dict[str, Any] = {
    "type": "box",
    "role": "add",
    "position": {"x": 0.0, "y": 0.0, "z": 5.0},
    "width": 40.0,
    "depth": 20.0,
    "height": 10.0,
}

CYLINDER_PRIMITIVE: dict[str, Any] = {
    "type": "cylinder",
    "role": "subtract",
    "position": {"x": 0.0, "y": 0.0, "z": 10.0},
    "diameter": 5.0,
    "height": 30.0,
}

#: The Planner's prompt-driven geometry: a box with a cylinder cut out of it.
PRIMITIVE_SPEC: dict[str, Any] = {
    **ModelSpecification.example(),
    "primitives": [BOX_PRIMITIVE, CYLINDER_PRIMITIVE],
}


def _deps(
    *,
    provider: FakeProvider | None = None,
    cad: FakeCADBackend | None = None,
    **overrides: Any,
) -> WorkflowDeps:
    """Build the fake workflow dependencies (never a live backend)."""
    overrides.setdefault("preview_renderer", lambda _stl: PREVIEW_PNG)
    return WorkflowDeps(
        provider=provider or FakeProvider(),
        cad_backend=cad or FakeCADBackend(),
        retrieve_fn=lambda *args, **kwargs: [],
        max_attempts=3,
        **overrides,
    )


def _install_task(monkeypatch, tmp_path, deps: WorkflowDeps) -> LocalStorage:
    """Point the task at the injected fakes and a per-test storage root."""
    monkeypatch.setattr("agents.tasks.build_dependencies", lambda: deps)
    storage = LocalStorage(root=tmp_path)
    monkeypatch.setattr("agents.tasks.get_storage", lambda: storage)
    return storage


def test_task_persists_primitives_and_hands_them_to_the_cad_backend(monkeypatch, tmp_path):
    project = ProjectFactory()
    provider = FakeProvider(
        plan={
            "specification": PRIMITIVE_SPEC,
            "needs_research": False,
            "research_query": None,
        }
    )
    cad = FakeCADBackend()
    _install_task(monkeypatch, tmp_path, _deps(provider=provider, cad=cad))

    run_id = run_agent_workflow(project.pk, PROMPT, project.created_by_id)

    assert AgentRun.objects.get(pk=run_id).status == AgentRunStatus.DONE

    version = project.versions.get()
    primitives = version.specification_json["primitives"]
    assert [primitive["type"] for primitive in primitives] == ["box", "cylinder"]
    assert [primitive["role"] for primitive in primitives] == ["add", "subtract"]
    assert primitives[0]["position"] == {"x": 0.0, "y": 0.0, "z": 5.0}
    assert primitives[0]["width"] == 40.0
    assert primitives[0]["depth"] == 20.0
    assert primitives[0]["height"] == 10.0
    assert primitives[1]["position"] == {"x": 0.0, "y": 0.0, "z": 10.0}
    assert primitives[1]["diameter"] == 5.0
    assert primitives[1]["height"] == 30.0

    # The CAD backend validated/rendered exactly the persisted specification.
    assert cad.validated_models
    assert cad.validated_models[0].specification["primitives"] == primitives


# ---------------------------------------------------------------------------
# 3. Vision review retry loop (task + persisted version)
# ---------------------------------------------------------------------------


def test_task_retries_once_on_a_vision_mismatch_then_finishes_done(monkeypatch, tmp_path):
    project = ProjectFactory()
    provider = FakeVisionProvider(
        reviews=[
            {"matches": False, "issues": ["the body is too short"], "summary": "mismatch"},
            {"matches": True, "issues": [], "summary": "ok"},
        ]
    )
    cad = FakeCADBackend()
    reviser_calls: list[tuple[dict[str, Any], list[str], int]] = []

    def reviser(specification: dict[str, Any], errors: list[str], attempt: int):
        reviser_calls.append((specification, errors, attempt))
        adjusted = dict(specification)
        adjusted["angle"] = 0.0
        return adjusted

    _install_task(
        monkeypatch,
        tmp_path,
        _deps(provider=provider, cad=cad, reviser=reviser),
    )

    run_id = run_agent_workflow(project.pk, PROMPT, project.created_by_id)

    run = AgentRun.objects.get(pk=run_id)
    assert run.status == AgentRunStatus.DONE
    assert run.error == ""

    # Exactly one review-driven retry: the mismatch fed the reviser and the CAD
    # node ran a second time.
    assert run.state_json["attempts"] == 2
    assert cad.generate_calls == 2
    assert cad.validate_calls == 2
    assert cad.export_calls == 2
    assert provider.calls == ["PlannerPlan", "ReviewResult", "ReviewResult"]
    assert reviser_calls and reviser_calls[0][1] == ["the body is too short"]
    assert reviser_calls[0][2] == 2

    version = project.versions.get()
    # The reviser's adjusted specification is what got persisted.
    assert version.specification_json["angle"] == 0.0
    # The (final) verdict is recorded on the version and the run, byte-free.
    assert version.validation_json["vision_used"] is True
    assert version.validation_json["vision_review"] == {
        "matches": True,
        "issues": [],
        "summary": "ok",
    }
    assert run.state_json["vision_review"] == version.validation_json["vision_review"]
    # The retry (and its mismatch verdict) is visible in the run history.
    assert any("review: mismatch, retrying" in entry for entry in run.state_json["history"])
    # The rendered preview was persisted next to the other artifacts.
    assert version.preview_image.name == f"projects/{project.pk}/v1/preview.png"


def test_task_without_vision_skips_the_review_and_never_renders(monkeypatch, tmp_path):
    """No-vision providers stay ``done`` with no retry and no preview.

    The node-level guard is already covered by
    ``agents/graph/tests/test_review.py::test_no_vision_skips_without_rendering``;
    this is the integration counterpart through the persisted task result.
    """
    project = ProjectFactory()
    provider = FakeProvider()  # supports_vision() is False
    cad = FakeCADBackend()
    rendered: list[bytes] = []

    def renderer(stl: bytes) -> bytes:
        rendered.append(stl)
        raise AssertionError("a text-only provider must not render a preview")

    _install_task(
        monkeypatch,
        tmp_path,
        _deps(provider=provider, cad=cad, preview_renderer=renderer),
    )

    run_id = run_agent_workflow(project.pk, PROMPT, project.created_by_id)

    assert AgentRun.objects.get(pk=run_id).status == AgentRunStatus.DONE
    assert rendered == []
    assert cad.generate_calls == 1  # no review-driven retry
    version = project.versions.get()
    assert not version.preview_image.name
    assert version.validation_json["vision_used"] is False
    assert version.validation_json["vision_review"] == {
        "skipped": True,
        "reason": "no_vision",
    }


# ---------------------------------------------------------------------------
# 4. Guard: a broken preview renderer must not fail the run
# ---------------------------------------------------------------------------


def test_task_survives_a_preview_render_failure(monkeypatch, tmp_path):
    """A raising renderer is a warning + skip; the valid model is still stored.

    The node-level ``test_render_failure_is_swallowed_into_a_warning`` already
    pins this; here we prove the run still persists a ``done`` version.
    """
    project = ProjectFactory()
    provider = FakeVisionProvider()  # vision-capable, but rendering will fail
    cad = FakeCADBackend()

    def boom(_stl: bytes) -> bytes:
        raise PreviewRenderError("degenerate mesh")

    _install_task(
        monkeypatch,
        tmp_path,
        _deps(provider=provider, cad=cad, preview_renderer=boom),
    )

    run_id = run_agent_workflow(project.pk, PROMPT, project.created_by_id)

    assert AgentRun.objects.get(pk=run_id).status == AgentRunStatus.DONE
    assert cad.generate_calls == 1  # no retry from a failed review
    assert provider.calls == ["PlannerPlan"]  # no vision call after a failed render

    version = project.versions.get()
    assert not version.preview_image.name
    assert version.validation_json["vision_used"] is False
    assert version.validation_json["vision_review"] == {
        "skipped": True,
        "reason": "render_failed",
    }
