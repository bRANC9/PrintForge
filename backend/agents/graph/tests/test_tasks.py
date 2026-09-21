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
from factories import ProjectFactory

from agents.graph import WorkflowDeps
from agents.graph.tests.fakes import FakeCADBackend, FakeProvider
from agents.llm import LLMError
from agents.models import AgentRun, AgentRunStatus
from agents.spec import ModelSpecification
from agents.tasks import NotificationKind, build_dependencies, run_agent_workflow
from files.services import LocalStorage

pytestmark = pytest.mark.django_db


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
    *, provider: FakeProvider | None = None, cad: FakeCADBackend | None = None
) -> WorkflowDeps:
    return WorkflowDeps(
        provider=provider or FakeProvider(),
        cad_backend=cad or FakeCADBackend(),
        retrieve_fn=lambda *args, **kwargs: [],
        max_attempts=3,
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


def test_task_without_user_leaves_created_by_empty(monkeypatch, tmp_path):
    project = ProjectFactory()
    _install(monkeypatch, tmp_path, _deps())

    run_id = run_agent_workflow(project.pk, "make a holder")

    assert AgentRun.objects.get(pk=run_id).status == AgentRunStatus.DONE
    assert project.versions.get().created_by_id is None


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
