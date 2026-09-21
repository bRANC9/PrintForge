"""``run_agent_workflow`` persistence tests (no live LLM, no OpenSCAD).

The graph dependencies are injected by monkeypatching
:func:`agents.tasks.build_dependencies`, and the storage root is redirected to a
temporary directory, so the real Celery task body runs synchronously.
"""

from __future__ import annotations

import json

import pytest
from factories import ProjectFactory

from agents.graph import WorkflowDeps
from agents.graph.tests.fakes import FakeCADBackend, FakeProvider
from agents.llm import LLMError
from agents.models import AgentRun, AgentRunStatus
from agents.spec import ModelSpecification
from agents.tasks import run_agent_workflow
from files.services import LocalStorage

pytestmark = pytest.mark.django_db


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
