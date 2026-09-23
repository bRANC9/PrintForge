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
from agents.graph.tests.fakes import DEFAULT_SPEC, FakeCADBackend, FakeProvider, FakeVisionProvider
from agents.llm import LLMError
from agents.models import AgentRun, AgentRunStatus
from agents.spec import ModelSpecification
from agents.tasks import NotificationKind, build_dependencies, run_agent_workflow
from designs.models import ModelVersionOrigin
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
