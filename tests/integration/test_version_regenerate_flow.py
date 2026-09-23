"""Version regeneration wired end to end (docs/version-history-controls.md).

``backend/api/tests/test_versions_regenerate_api.py`` pins the endpoint's
response and the enqueued kwargs; ``backend/designs/tests/test_regenerate.py``
pins the service in isolation. This module covers the missing seam: the API
enqueues a job, the *real* task body then runs (with fakes) and the persisted
``ModelVersion`` records the provenance -- ``parent_version`` plus the ``origin``
for a reroll, an edited prompt and a manual specification.
"""

from __future__ import annotations

from typing import Any

import pytest
from factories import ModelVersionFactory
from rest_framework.test import APIClient

from agents.graph import WorkflowDeps
from agents.graph.tests.fakes import FakeCADBackend, FakeProvider
from agents.tasks import run_agent_workflow
from designs.models import ModelVersionOrigin
from designs.tasks import render_model_stl
from files.services import LocalStorage

pytestmark = pytest.mark.django_db


@pytest.fixture
def broker(monkeypatch):
    """Capture enqueues instead of talking to the Celery broker."""
    calls: dict[str, list] = {"agent": [], "render": []}
    monkeypatch.setattr(
        run_agent_workflow,
        "delay",
        lambda *args, **kwargs: calls["agent"].append((args, kwargs)),
    )
    monkeypatch.setattr(render_model_stl, "delay", lambda pk: calls["render"].append(pk))
    return calls


@pytest.fixture
def base_version():
    return ModelVersionFactory(prompt="make a phone holder")


@pytest.fixture
def client(base_version):
    api = APIClient()
    api.force_authenticate(user=base_version.project.created_by)
    return api


def _run_deps(monkeypatch, tmp_path, provider: FakeProvider | None = None) -> FakeProvider:
    provider = provider or FakeProvider()
    deps = WorkflowDeps(
        provider=provider,
        cad_backend=FakeCADBackend(),
        retrieve_fn=lambda *args, **kwargs: [],
        preview_renderer=lambda _stl: b"",
        max_attempts=3,
    )
    monkeypatch.setattr("agents.tasks.build_dependencies", lambda: deps)
    monkeypatch.setattr("agents.tasks.get_storage", lambda: LocalStorage(root=tmp_path))
    return provider


def test_regenerate_reroll_runs_the_task_and_records_parent_and_origin(
    monkeypatch, tmp_path, client, base_version, broker
):
    response = client.post(f"/api/v1/versions/{base_version.pk}/regenerate/", {}, format="json")

    assert response.status_code == 202, response.content
    assert response.json() == {"queued": True, "parent_version": base_version.pk}
    ((args, kwargs),) = broker["agent"]
    assert args == ()
    assert kwargs["base_version_id"] == base_version.pk
    assert kwargs["prompt"] == base_version.prompt
    assert kwargs["regenerate"] is True

    _run_deps(monkeypatch, tmp_path)
    run_agent_workflow(*args, **kwargs)

    derived = base_version.project.versions.exclude(pk=base_version.pk).get()
    assert derived.parent_version_id == base_version.pk
    assert derived.origin == ModelVersionOrigin.REGENERATE
    assert derived.prompt == base_version.prompt


def test_regenerate_with_an_edited_prompt_keeps_it_on_the_derived_version(
    monkeypatch, tmp_path, client, base_version, broker
):
    response = client.post(
        f"/api/v1/versions/{base_version.pk}/regenerate/",
        {"prompt": "make it much bigger"},
        format="json",
    )

    assert response.status_code == 202, response.content
    ((args, kwargs),) = broker["agent"]
    assert kwargs["prompt"] == "make it much bigger"

    _run_deps(monkeypatch, tmp_path)
    run_agent_workflow(*args, **kwargs)

    derived = base_version.project.versions.exclude(pk=base_version.pk).get()
    assert derived.parent_version_id == base_version.pk
    assert derived.origin == ModelVersionOrigin.REGENERATE
    assert derived.prompt == "make it much bigger"


def test_regenerate_with_a_manual_specification_is_a_derived_manual_render(
    client, base_version, broker
):
    specification: dict[str, Any] = {"object": "cube", "dimensions": {"width": 1.0}}

    response = client.post(
        f"/api/v1/versions/{base_version.pk}/regenerate/",
        {"specification_json": specification},
        format="json",
    )

    assert response.status_code == 202, response.content
    derived = base_version.project.versions.exclude(pk=base_version.pk).get()
    assert derived.parent_version_id == base_version.pk
    assert derived.origin == ModelVersionOrigin.MANUAL
    assert derived.prompt == base_version.prompt
    assert derived.specification_json == specification
    # The manual path reuses the synchronous render enqueue, not the agent.
    assert broker["render"] == [derived.pk]
    assert broker["agent"] == []
