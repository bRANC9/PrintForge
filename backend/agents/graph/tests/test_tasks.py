"""``run_agent_workflow`` persistence + notification tests.

No live LLM, no OpenSCAD, no broker: the graph dependencies are injected by
monkeypatching :func:`agents.tasks.build_dependencies`, the notification
fan-out is recorded, and the storage root is redirected to a temporary
directory, so the real Celery task body runs synchronously.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.conf import settings
from factories import ModelVersionFactory, ProjectFactory

from agents.graph import WorkflowDeps
from agents.graph.tests.fakes import (
    DEFAULT_SPEC,
    FakeCADBackend,
    FakeProvider,
    FakeTextReviewProvider,
    FakeVisionProvider,
)
from agents.llm import LLMError
from agents.models import AgentRun, AgentRunStatus
from agents.spec import ModelSpecification
from agents.tasks import NotificationKind, build_dependencies, run_agent_workflow
from designs.cad.pipeline import dimension_warnings
from designs.models import ModelVersionOrigin
from designs.services import version_status
from files.services import LocalStorage

pytestmark = pytest.mark.django_db

PREVIEW_PNG = b"\x89PNG\r\n\x1a\nfake-preview"


class NotifyRecorder:
    """Records ``notify_project_members`` calls; optionally raises."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def __call__(self, **kwargs: Any) -> list[Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return []


def _deps(
    *,
    provider: FakeProvider | None = None,
    cad: FakeCADBackend | None = None,
    **overrides: Any,
) -> WorkflowDeps:
    overrides.setdefault("review_text_provider", FakeTextReviewProvider())
    return WorkflowDeps(
        provider=provider or FakeProvider(),
        cad_backend=cad or FakeCADBackend(),
        retrieve_fn=lambda *args, **kwargs: [],
        max_attempts=3,
        **overrides,
    )


def _install(monkeypatch, tmp_path, deps: WorkflowDeps) -> LocalStorage:
    monkeypatch.setattr("agents.tasks.build_dependencies", lambda: deps)
    storage = LocalStorage(root=tmp_path)
    monkeypatch.setattr("agents.tasks.get_storage", lambda: storage)
    return storage


def test_task_persists_the_llm_trace(monkeypatch, tmp_path):
    """The main generator's input/output is stored for the UI (llm_trace)."""
    project = ProjectFactory()
    provider = FakeProvider()
    _install(monkeypatch, tmp_path, _deps(provider=provider))

    run_id = run_agent_workflow(project.pk, "make a phone holder", project.created_by_id)

    run = AgentRun.objects.get(pk=run_id)
    trace = run.state_json["llm_trace"]
    assert trace
    assert trace[0]["agent"] == "planner"
    assert "phone holder" in trace[0]["prompt"]
    assert trace[0]["response"]["object"] == "phone_holder"

    version = project.versions.get()
    assert version.validation_json["llm_trace"][0]["agent"] == "planner"


def test_task_persists_run_version_and_artifacts(monkeypatch, tmp_path):
    project = ProjectFactory()
    provider = FakeProvider()
    cad = FakeCADBackend()
    storage = _install(monkeypatch, tmp_path, _deps(provider=provider, cad=cad))

    run_id = run_agent_workflow(project.pk, "make a phone holder", project.created_by_id)
    run = AgentRun.objects.get(pk=run_id)
    assert run.status == AgentRunStatus.DONE
    assert run.error == ""
    assert run.state_json["version_id"] == project.versions.get().pk
    assert run.state_json["attempts"] == 1

    version = project.versions.get()
    assert version.version == 1
    assert version.specification_json == ModelSpecification.example()
    assert version.created_by_id == project.created_by_id
    assert version.validation_json["status"] == "done"
    assert version.validation_json["agent_run_id"] == run_id

    # The CAD artifacts are written through the storage backend and linked.
    assert version.scad_file.name == f"projects/{project.pk}/v1/model.scad"
    assert version.stl_file.name == f"projects/{project.pk}/v1/model.stl"
    assert storage.read_bytes(version.scad_file.name) == cad.scad_source.encode("utf-8")
    assert storage.read_bytes(version.stl_file.name) == cad.stl_bytes


def test_task_persists_the_preview_image_and_vision_review(monkeypatch, tmp_path):
    """The vision self-check's PNG and verdict are stored (docs/vision-self-check.md 5.)."""
    project = ProjectFactory()
    provider = FakeVisionProvider(reviews=[{"matches": True, "issues": [], "summary": "ok"}])
    deps = _deps(
        provider=provider,
        preview_renderer=lambda _stl: PREVIEW_PNG,
    )
    storage = _install(monkeypatch, tmp_path, deps)

    run_id = run_agent_workflow(project.pk, "make a holder", project.created_by_id)

    run = AgentRun.objects.get(pk=run_id)
    assert run.status == AgentRunStatus.DONE
    assert run.state_json["vision_used"] is True
    assert run.state_json["vision_review"] == {"matches": True, "issues": [], "summary": "ok"}

    version = project.versions.get()
    preview_rel = f"projects/{project.pk}/v1/preview.png"
    assert version.preview_image.name == preview_rel
    assert storage.read_bytes(preview_rel) == PREVIEW_PNG
    assert version.validation_json["preview_file"] == preview_rel
    assert version.validation_json["vision_used"] is True
    assert version.validation_json["vision_review"]["matches"] is True


def test_task_without_vision_has_no_preview_file(monkeypatch, tmp_path):
    project = ProjectFactory()
    _install(monkeypatch, tmp_path, _deps())

    run_id = run_agent_workflow(project.pk, "make a holder", project.created_by_id)

    assert AgentRun.objects.get(pk=run_id).status == AgentRunStatus.DONE
    version = project.versions.get()
    assert not version.preview_image.name
    assert "preview_file" not in version.validation_json
    assert version.validation_json["vision_used"] is False


def test_task_without_user_leaves_created_by_empty(monkeypatch, tmp_path):
    project = ProjectFactory()
    _install(monkeypatch, tmp_path, _deps())

    run_id = run_agent_workflow(project.pk, "make a holder")

    assert AgentRun.objects.get(pk=run_id).status == AgentRunStatus.DONE
    assert project.versions.get().created_by_id is None


def test_task_loads_a_reference_image_from_a_storage_path(monkeypatch, tmp_path):
    """The API stores the upload and passes its path; the task seeds from it."""
    project = ProjectFactory()
    storage = _install(monkeypatch, tmp_path, _deps())
    name = f"reference_uploads/{project.pk}/photo.png"
    storage.write_bytes(name, b"\x89PNG-fake")

    run_id = run_agent_workflow(
        project.pk,
        "make a holder",
        project.created_by_id,
        reference_image_name=name,
        reference_note="70 mm wide",
    )

    run = AgentRun.objects.get(pk=run_id)
    assert run.status == AgentRunStatus.DONE
    assert run.state_json["reference_image_provided"] is True
    version = project.versions.get()
    assert version.reference_note == "70 mm wide"
    assert "photo" in version.reference_image.name
    assert version.reference_image.name.endswith(".png")


def test_task_marks_run_failed_when_validation_exhausts_retries(monkeypatch, tmp_path):
    project = ProjectFactory()
    cad = FakeCADBackend(failures=99)
    _install(monkeypatch, tmp_path, _deps(cad=cad))

    run_id = run_agent_workflow(project.pk, "impossible part", project.created_by_id)

    run = AgentRun.objects.get(pk=run_id)
    assert run.status == AgentRunStatus.FAILED
    assert run.state_json == {} or run.state_json is not None
    error = json.loads(run.error)
    assert error["stage"] == "validator"
    assert "3 attempt(s)" in error["message"]

    # A failed run never leaves a model version behind.
    assert project.versions.count() == 0
    assert cad.generate_calls == 3


def test_task_marks_run_failed_on_planner_error(monkeypatch, tmp_path):
    project = ProjectFactory()
    provider = FakeProvider(planner_error=LLMError("ollama offline"))
    _install(monkeypatch, tmp_path, _deps(provider=provider))

    run_id = run_agent_workflow(project.pk, "x", project.created_by_id)

    run = AgentRun.objects.get(pk=run_id)
    assert run.status == AgentRunStatus.FAILED
    error = json.loads(run.error)
    assert error["stage"] == "planner"
    assert "ollama offline" in error["message"]
    assert project.versions.count() == 0


def test_task_never_leaves_a_run_running_on_a_crash(monkeypatch):
    project = ProjectFactory()

    def boom() -> WorkflowDeps:
        raise RuntimeError("dependency factory exploded")

    monkeypatch.setattr("agents.tasks.build_dependencies", boom)

    run_id = run_agent_workflow(project.pk, "x", project.created_by_id)

    run = AgentRun.objects.get(pk=run_id)
    assert run.status == AgentRunStatus.FAILED
    assert "dependency factory exploded" in run.error
    assert run.completed_at is not None


def test_task_marks_run_failed_when_persistence_fails(monkeypatch, tmp_path):
    project = ProjectFactory()
    _install(monkeypatch, tmp_path, _deps())

    def boom(**kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr("agents.tasks.create_next_version", boom)

    run_id = run_agent_workflow(project.pk, "x", project.created_by_id)

    run = AgentRun.objects.get(pk=run_id)
    assert run.status == AgentRunStatus.FAILED
    error = json.loads(run.error)
    assert error["stage"] == "persist"
    assert "disk full" in error["message"]
    assert project.versions.count() == 0


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def test_task_notifies_project_members_on_success(monkeypatch, tmp_path):
    project = ProjectFactory()
    _install(monkeypatch, tmp_path, _deps())
    recorder = NotifyRecorder()
    monkeypatch.setattr("agents.tasks.notify_project_members", recorder)

    run_id = run_agent_workflow(project.pk, "make a holder", project.created_by_id)

    assert AgentRun.objects.get(pk=run_id).status == AgentRunStatus.DONE
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["kind"] == NotificationKind.AGENT_DONE
    assert call["project"] == project
    assert call["url"] == f"/projects/{project.pk}/"
    assert call["exclude"].pk == project.created_by_id
    assert "generated" in call["message"].lower()


def test_task_notifies_project_members_on_failure(monkeypatch, tmp_path):
    project = ProjectFactory()
    cad = FakeCADBackend(failures=99)
    _install(monkeypatch, tmp_path, _deps(cad=cad))
    recorder = NotifyRecorder()
    monkeypatch.setattr("agents.tasks.notify_project_members", recorder)

    run_id = run_agent_workflow(project.pk, "impossible part", project.created_by_id)

    assert AgentRun.objects.get(pk=run_id).status == AgentRunStatus.FAILED
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["kind"] == NotificationKind.AGENT_FAILED
    assert call["url"] == f"/projects/{project.pk}/"
    assert call["exclude"].pk == project.created_by_id
    assert "failed" in call["message"].lower()
    assert "3 attempt(s)" in call["message"]  # the short structured reason


def test_notification_failure_does_not_change_a_successful_run(monkeypatch, tmp_path):
    project = ProjectFactory()
    _install(monkeypatch, tmp_path, _deps())
    monkeypatch.setattr(
        "agents.tasks.notify_project_members",
        NotifyRecorder(error=RuntimeError("notify backend down")),
    )

    run_id = run_agent_workflow(project.pk, "make a holder", project.created_by_id)

    run = AgentRun.objects.get(pk=run_id)
    assert run.status == AgentRunStatus.DONE
    assert project.versions.count() == 1
    assert project.versions.get().validation_json["status"] == "done"


def test_notification_failure_does_not_change_a_failed_run(monkeypatch, tmp_path):
    project = ProjectFactory()
    cad = FakeCADBackend(failures=99)
    _install(monkeypatch, tmp_path, _deps(cad=cad))
    monkeypatch.setattr(
        "agents.tasks.notify_project_members",
        NotifyRecorder(error=RuntimeError("notify backend down")),
    )

    run_id = run_agent_workflow(project.pk, "impossible part", project.created_by_id)

    assert AgentRun.objects.get(pk=run_id).status == AgentRunStatus.FAILED
    assert project.versions.count() == 0


# ---------------------------------------------------------------------------
# RAG wiring
# ---------------------------------------------------------------------------


def test_build_dependencies_injects_the_embeddings_retrieve_service():
    from embeddings.services import retrieve as embeddings_retrieve

    deps = build_dependencies()

    # The Research node must receive the real RAG entry point explicitly; that
    # function itself respects settings.RAG_ENABLED (returns [] when disabled).
    assert deps.retrieve_fn is embeddings_retrieve
    assert deps.max_attempts == settings.AGENT_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Dedicated vision provider (docs/vision-self-check.md)
# ---------------------------------------------------------------------------


def test_build_dependencies_without_a_vision_model_has_no_review_provider(monkeypatch):
    """An empty ``ollama_vision_model`` keeps the review on the main provider."""
    monkeypatch.setattr("agents.tasks.get_setting", lambda name: "")
    monkeypatch.setattr("agents.tasks.get_provider", lambda **kwargs: FakeProvider())

    deps = build_dependencies()

    assert deps.review_provider is None


def test_build_dependencies_builds_the_review_provider_with_the_vision_model(monkeypatch):
    """A configured ``ollama_vision_model`` builds a dedicated review provider."""
    main = FakeProvider()
    vision = FakeVisionProvider()
    calls: list[dict[str, Any]] = []

    def fake_provider(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return vision if kwargs.get("model") else main

    monkeypatch.setattr("agents.tasks.get_provider", fake_provider)
    monkeypatch.setattr("agents.tasks.get_setting", lambda name: "llava:13b")

    deps = build_dependencies()

    # The main provider is built without a model override; the review provider
    # receives the configured vision model (the base URL stays provider-resolved).
    assert deps.provider is main
    assert deps.review_provider is vision
    assert calls == [{}, {"model": "llava:13b"}]


# ---------------------------------------------------------------------------
# Visual-prompt edits (docs/visual-editing.md 3.5)
# ---------------------------------------------------------------------------

ANNOTATIONS: list[dict[str, Any]] = [
    {
        "id": "c1",
        "kind": "point",
        "point": [35.0, 12.5, 8.0],
        "normal": [0.0, 0.0, 1.0],
        "faces": [],
        "instruction": "4 mm-es átmenő lyuk",
    }
]


def test_task_persists_an_edit_version_with_parent_and_annotations(monkeypatch, tmp_path):
    project = ProjectFactory()
    base = ModelVersionFactory(project=project, version=1)
    _install(monkeypatch, tmp_path, _deps())

    run_id = run_agent_workflow(
        project.pk,
        "A kijelölt peremre tegyél egy lyukat.",
        project.created_by_id,
        base_version_id=base.pk,
        annotations=ANNOTATIONS,
    )

    run = AgentRun.objects.get(pk=run_id)
    assert run.status == AgentRunStatus.DONE
    # Only the annotation count reaches the run summary, never the raw payload.
    assert run.state_json["annotation_count"] == len(ANNOTATIONS)

    new_version = project.versions.get(version=2)
    assert new_version.parent_version_id == base.pk
    assert new_version.annotations_json == ANNOTATIONS
    assert new_version.validation_json["parent_version"] == base.pk
    assert new_version.validation_json["annotation_count"] == len(ANNOTATIONS)


def test_task_ignores_a_missing_or_foreign_base_version(monkeypatch, tmp_path):
    project = ProjectFactory()
    other_project = ProjectFactory()
    foreign = ModelVersionFactory(project=other_project, version=1)
    _install(monkeypatch, tmp_path, _deps())

    run_id = run_agent_workflow(
        project.pk,
        "make a holder",
        project.created_by_id,
        base_version_id=foreign.pk,
        annotations=ANNOTATIONS,
    )

    run = AgentRun.objects.get(pk=run_id)
    assert run.status == AgentRunStatus.DONE
    # A foreign base is ignored: the run falls back to a fresh generation.
    version = project.versions.get()
    assert version.parent_version_id is None
    assert version.annotations_json == []
    assert run.state_json["annotation_count"] == 0


# ---------------------------------------------------------------------------
# Planner clarifications / assumptions (docs/planner-clarification.md 4.)
# ---------------------------------------------------------------------------

BLOCKING_QUESTION = {
    "question": "Which phone model is the holder for?",
    "answer": "",
    "kind": "needs_user_input",
    "field": "dimensions",
}
ASSUMED_VALUE = {
    "question": "No wall thickness given; assuming 4 mm.",
    "answer": "4",
    "kind": "assumed",
    "field": "wall_thickness",
}


def _plan(*clarifications: dict[str, Any]) -> dict[str, Any]:
    return {
        "specification": DEFAULT_SPEC,
        "needs_research": False,
        "research_query": None,
        "clarifications": list(clarifications),
    }


def test_task_clarification_finishes_done_without_a_version(monkeypatch, tmp_path):
    project = ProjectFactory()
    provider = FakeProvider(plan=_plan(BLOCKING_QUESTION))
    _install(monkeypatch, tmp_path, _deps(provider=provider))
    recorder = NotifyRecorder()
    monkeypatch.setattr("agents.tasks.notify_project_members", recorder)

    run_id = run_agent_workflow(
        project.pk,
        "make a holder",
        project.created_by_id,
        clarify_policy="ask",
    )

    run = AgentRun.objects.get(pk=run_id)
    assert run.status == AgentRunStatus.DONE
    assert project.versions.count() == 0  # never finalised -> no version
    assert run.state_json["status"] == "clarification"
    assert run.state_json["clarification_count"] == 1
    assert run.state_json["clarifications"] == [
        {"question": BLOCKING_QUESTION["question"], "field": "dimensions"}
    ]
    # The blocking question carries no answer; assumptions are empty.
    assert "answer" not in run.state_json["clarifications"][0]
    assert run.state_json["assumption_count"] == 0
    assert run.state_json["assumptions"] == []
    # Stopping for clarification is not a failure: no failure notification.
    assert recorder.calls == []


def test_task_clarification_without_ask_policy_creates_a_version(monkeypatch, tmp_path):
    project = ProjectFactory()
    provider = FakeProvider(plan=_plan(BLOCKING_QUESTION))
    _install(monkeypatch, tmp_path, _deps(provider=provider))

    run_id = run_agent_workflow(project.pk, "make a holder", project.created_by_id)

    run = AgentRun.objects.get(pk=run_id)
    assert run.status == AgentRunStatus.DONE
    assert project.versions.count() == 1
    assert run.state_json["clarification_count"] == 0


def test_task_persists_assumptions_and_marks_review_required(monkeypatch, tmp_path):
    project = ProjectFactory()
    provider = FakeProvider(plan=_plan(ASSUMED_VALUE))
    _install(monkeypatch, tmp_path, _deps(provider=provider))

    run_id = run_agent_workflow(project.pk, "make a holder", project.created_by_id)

    run = AgentRun.objects.get(pk=run_id)
    assert run.status == AgentRunStatus.DONE
    assert run.state_json["assumption_count"] == 1
    assert run.state_json["assumptions"] == [
        {"field": "wall_thickness", "question": ASSUMED_VALUE["question"], "answer": "4"}
    ]
    version = project.versions.get()
    assert version.validation_json["review_required"] is True
    assert version.validation_json["assumptions"] == [
        {"field": "wall_thickness", "question": ASSUMED_VALUE["question"], "answer": "4"}
    ]


def test_task_without_assumptions_has_no_review_flag(monkeypatch, tmp_path):
    project = ProjectFactory()
    _install(monkeypatch, tmp_path, _deps())

    run_agent_workflow(project.pk, "make a holder", project.created_by_id)

    version = project.versions.get()
    assert "assumptions" not in version.validation_json
    assert "review_required" not in version.validation_json


# ---------------------------------------------------------------------------
# Regeneration provenance (docs/version-history-controls.md 2.)
# ---------------------------------------------------------------------------


def test_task_regenerate_records_origin_and_parent(monkeypatch, tmp_path):
    project = ProjectFactory()
    base = ModelVersionFactory(project=project, version=1)
    _install(monkeypatch, tmp_path, _deps())

    run_id = run_agent_workflow(
        project.pk,
        "make a bigger holder",
        project.created_by_id,
        base_version_id=base.pk,
        regenerate=True,
    )

    assert AgentRun.objects.get(pk=run_id).status == AgentRunStatus.DONE
    derived = project.versions.get(version=2)
    assert derived.origin == ModelVersionOrigin.REGENERATE
    assert derived.parent_version_id == base.pk


def test_task_without_regenerate_uses_the_generate_origin(monkeypatch, tmp_path):
    project = ProjectFactory()
    _install(monkeypatch, tmp_path, _deps())

    run_agent_workflow(project.pk, "make a holder", project.created_by_id)

    assert project.versions.get().origin == ModelVersionOrigin.GENERATE


# ---------------------------------------------------------------------------
# Skill provenance (docs/skills.md 7.)
# ---------------------------------------------------------------------------

SKILL = {
    "id": 1,
    "slug": "cookie-cutter",
    "name": "Süti kinyomó",
    "kind": "guidance",
    "template_key": "",
    "object_kind": "cookie_cutter",
    "description": "",
    "guidance": "",
    "defaults_json": {},
    "constraints_json": {"min_wall_mm": 0.4},
}


def test_task_persists_the_selected_skills(monkeypatch, tmp_path):
    project = ProjectFactory()
    deps = WorkflowDeps(
        provider=FakeProvider(),
        cad_backend=FakeCADBackend(),
        retrieve_fn=lambda *args, **kwargs: [],
        max_attempts=3,
        select_skills_fn=lambda **kwargs: [dict(SKILL)],
    )
    _install(monkeypatch, tmp_path, deps)

    run_id = run_agent_workflow(
        project.pk,
        "make a cookie cutter",
        project.created_by_id,
        auto_skill_selection=True,
    )

    run = AgentRun.objects.get(pk=run_id)
    assert run.state_json["skill_selection"] == "auto"
    assert run.state_json["skills"] == [
        {"slug": "cookie-cutter", "name": "Süti kinyomó", "selection": "auto"}
    ]
    version = project.versions.get()
    assert version.validation_json["skills"] == [
        {"slug": "cookie-cutter", "name": "Süti kinyomó", "selection": "auto"}
    ]
    assert version.validation_json["skill_selection"] == "auto"


def test_task_without_skills_has_no_skill_provenance(monkeypatch, tmp_path):
    project = ProjectFactory()
    _install(monkeypatch, tmp_path, _deps())

    run_agent_workflow(project.pk, "make a holder", project.created_by_id)

    version = project.versions.get()
    assert "skills" not in version.validation_json
    assert "skill_selection" not in version.validation_json


# ---------------------------------------------------------------------------
# The dimension check on the agent path
# ---------------------------------------------------------------------------
#
# ``designs.cad.pipeline.render_version`` runs the envelope check for a
# re-render and ``designs.services`` records the findings of an imported mesh,
# but the agent path never goes through ``render_version``: the Validator node
# exports the STL and ``agents.tasks._persist_version`` builds
# ``validation_json`` itself around that already-exported bytes. The check is
# invoked there too, because a prompt is the one case that matters: the measured
# 90 mm cookie press rendered 195 mm tall and every meshcheck said it was fine.
#
# No OpenSCAD and no stubbed check here: ``FakeCADBackend`` exports a real,
# measurable trimesh STL, so the verdict comes from a genuine mesh, and the
# assertions are the exact strings ``validation_json`` has to carry for the
# poller and the UI.


def _trimesh_stl(extents, *, lift=None):
    """A real, parseable STL of a box resting on the build plate by default."""
    import trimesh

    mesh = trimesh.creation.box(extents=extents)
    mesh.apply_translation([0.0, 0.0, lift if lift is not None else extents[2] / 2])
    return mesh.export(file_type="stl")


def _widget_plan():
    """A planner plan for a plain 40 x 60 x 10 mm widget.

    No guard keyword in the object name, and a primitive that matches the
    declared envelope, so the consistency guard stays quiet and the *only*
    possible finding is the dimension check under test.
    """
    return {
        "specification": {
            "object": "widget",
            "dimensions": {"width": 40.0, "height": 60.0, "thickness": 10.0},
            "angle": 15,
            "wall_thickness": 4,
            "mounting": {"type": "M5", "count": 2},
            "material": "PETG",
            "primitives": [
                {
                    "type": "box",
                    "role": "add",
                    "width": 40.0,
                    "depth": 60.0,
                    "height": 10.0,
                    "position": {"x": 0.0, "y": 0.0, "z": 5.0},
                }
            ],
        },
        "needs_research": False,
        "research_query": None,
    }


def _run_widget(monkeypatch, tmp_path, plan, stl_bytes):
    """Run the real task for a prompt whose backend exports *stl_bytes*.

    Returns the finished ``AgentRun`` and the persisted ``ModelVersion``. The
    preview renderer is stubbed so a real mesh never reaches the image
    renderer: the dimension check reads the same bytes either way.
    """
    project = ProjectFactory()
    storage = _install(
        monkeypatch,
        tmp_path,
        _deps(
            provider=FakeProvider(plan=plan),
            cad=FakeCADBackend(stl_bytes=stl_bytes),
            preview_renderer=lambda _stl: b"",
        ),
    )

    run_id = run_agent_workflow(project.pk, "make a 40 x 60 x 10 mm widget", project.created_by_id)

    return AgentRun.objects.get(pk=run_id), project.versions.get(), storage


def test_the_agent_path_records_a_rendered_envelope_that_contradicts_the_request(
    monkeypatch, tmp_path
):
    """The headline case: a 100 mm part declared as a 40 mm one, from a prompt.

    ``render_version`` covers a re-render and the mesh import covers a manual
    upload, so without the call in ``_persist_version`` this version would ship
    with an *empty* ``warnings`` list -- a watertight, printable, wrong part
    that looks finished. This test fails if the check is removed from the agent
    path, pointed at the wrong bytes, or pointed at anything but the declared
    ``dimensions``.
    """
    run, version, _storage = _run_widget(
        monkeypatch, tmp_path, _widget_plan(), _trimesh_stl((100.0, 60.0, 10.0))
    )

    # Advisory: the job still finishes done, exactly as a re-render does.
    assert run.status == AgentRunStatus.DONE
    assert version.validation_json["status"] == "done"
    assert version.validation_json["errors"] == []
    assert version.validation_json["warnings"] == [
        "rendered X extent 100.0mm differs from the requested width 40.0mm by +150% "
        "(tolerance +-25%)"
    ]


def test_the_agent_path_check_measures_the_persisted_stl_against_the_declared_dimensions(
    monkeypatch, tmp_path
):
    """The seam itself: the bytes on ``stl_file`` and the requested envelope.

    A second export, a re-measured source file or a different dimension mapping
    would all produce a plausible-looking warning for the wrong comparison; this
    pins the two arguments.
    """
    seen: list[tuple[bytes, object]] = []
    real_check = dimension_warnings

    def spy(payload, dimensions):  # noqa: ANN001
        seen.append((payload, dimensions))
        return real_check(payload, dimensions)

    monkeypatch.setattr("agents.tasks.dimension_warnings", spy)
    stl_bytes = _trimesh_stl((100.0, 60.0, 10.0))

    _run, version, storage = _run_widget(monkeypatch, tmp_path, _widget_plan(), stl_bytes)

    assert seen == [(stl_bytes, {"width": 40.0, "height": 60.0, "thickness": 10.0})]
    # ... and those bytes are the artifact the viewer will load.
    assert storage.read_bytes(version.stl_file.name) == seen[0][0]


def test_the_agent_path_reports_a_part_that_does_not_rest_on_the_build_plate(monkeypatch, tmp_path):
    """The build-plate half of the check travels on the agent path too.

    Every axis matches the request here, so the finding can only come from
    ``min Z != 0`` -- a primitive placed on its centre sinks into the plate.
    """
    run, version, _storage = _run_widget(
        monkeypatch, tmp_path, _widget_plan(), _trimesh_stl((40.0, 60.0, 10.0), lift=10.0)
    )

    assert run.status == AgentRunStatus.DONE
    assert version.validation_json["warnings"] == [
        "rendered part starts at Z=5.000mm; the rest of the app assumes the part "
        "rests on the build plate (min Z = 0)"
    ]


def test_a_matching_agent_render_adds_no_warnings_key(monkeypatch, tmp_path):
    """A check that fires on good output is worse than no check.

    The exact-envelope render must leave ``validation_json`` without a
    ``warnings`` key at all, so the UI can tell "nothing to say" from "something
    to say" instead of showing an empty advisory list for every version.
    """
    run, version, _storage = _run_widget(
        monkeypatch, tmp_path, _widget_plan(), _trimesh_stl((40.0, 60.0, 10.0))
    )

    assert run.status == AgentRunStatus.DONE
    assert "warnings" not in version.validation_json
    assert version.validation_json["status"] == "done"


def test_a_derived_edit_version_gets_the_dimension_check_too(monkeypatch, tmp_path):
    """A visual edit writes its ``validation_json`` through the same code.

    The derived version declares its own envelope, so it can contradict its own
    render exactly like a fresh generation's -- the check may not be gated on
    the origin, the parent link or the annotation payload.
    """
    project = ProjectFactory()
    base = ModelVersionFactory(project=project, version=1)
    _install(
        monkeypatch,
        tmp_path,
        _deps(
            provider=FakeProvider(plan=_widget_plan()),
            cad=FakeCADBackend(stl_bytes=_trimesh_stl((100.0, 60.0, 10.0))),
            preview_renderer=lambda _stl: b"",
        ),
    )

    run_id = run_agent_workflow(
        project.pk,
        "A kijelölt peremre tegyél egy lyukat.",
        project.created_by_id,
        base_version_id=base.pk,
        annotations=ANNOTATIONS,
    )

    assert AgentRun.objects.get(pk=run_id).status == AgentRunStatus.DONE
    derived = project.versions.get(version=2)
    assert derived.parent_version_id == base.pk
    assert derived.annotations_json == ANNOTATIONS
    assert derived.validation_json["warnings"] == [
        "rendered X extent 100.0mm differs from the requested width 40.0mm by +150% "
        "(tolerance +-25%)"
    ]


def test_an_unmeasurable_agent_stl_is_not_a_run_failure(monkeypatch, tmp_path):
    """The check is a diagnostic on a mesh that already exported.

    A payload trimesh cannot parse yields no findings and no key -- never a
    failed run and never a version stuck without a status.
    """
    run, version, _storage = _run_widget(
        monkeypatch, tmp_path, _widget_plan(), b"solid fake\nendsolid fake\n"
    )

    assert run.status == AgentRunStatus.DONE
    assert version.validation_json["status"] == "done"
    assert "warnings" not in version.validation_json


def test_the_agent_path_warnings_reach_the_status_poller(monkeypatch, tmp_path):
    """The end of the chain: what the UI polls carries the finding.

    ``version_status`` is the only thing between the persisted ``warnings`` and
    the browser, so a key that never leaves ``validation_json`` is a check
    nobody sees.
    """
    _run, version, _storage = _run_widget(
        monkeypatch, tmp_path, _widget_plan(), _trimesh_stl((100.0, 60.0, 10.0))
    )

    assert version_status(version)["warnings"] == [
        "rendered X extent 100.0mm differs from the requested width 40.0mm by +150% "
        "(tolerance +-25%)"
    ]
