"""Tests for the in-process MCP tool implementations.

Run explicitly with ``uv run pytest mcp/tests``: the backend ``testpaths``
setting only collects the top-level ``../tests`` directory.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from designs.models import ModelVersion
from designs.tasks import render_model_stl
from files.services import LocalStorage
from mcp.tools import all_tools, call, register
from projects.services import create_project
from workspaces.services import create_workspace

User = get_user_model()

NEW_TOOLS = {"generate_model_from_prompt", "get_model_version", "export_model_stl"}


def test_new_tools_are_registered():
    names = set(all_tools())
    assert {"create_workspace", "create_project", "list_projects"} | NEW_TOOLS <= names


def test_duplicate_registration_raises():
    with pytest.raises(ValueError):
        register("list_projects", "duplicate")(lambda **kwargs: None)


def test_call_forwards_a_parameter_named_name(project):
    # Regression: the dispatcher's tool identifier is positional-only, so a tool
    # parameter literally named ``name`` (create_workspace) must not collide.
    result = call("create_workspace", name="Lab", owner_id=project.created_by_id)

    assert result["name"] == "Lab"


@pytest.fixture
def project(db):
    user = User.objects.create_user(username="ada", email="ada@example.com", password="pw")
    workspace = create_workspace(name="Lab", owner=user)
    return create_project(workspace=workspace, name="Holder", created_by=user)


def test_generate_model_from_prompt_creates_and_enqueues(project, monkeypatch):
    queued: list[int] = []
    monkeypatch.setattr(render_model_stl, "delay", queued.append)

    result = call("generate_model_from_prompt", project_id=project.id, prompt="a box")

    version = ModelVersion.objects.get(pk=result["version_id"])
    assert result == {"version_id": version.id, "version": 1, "status": "queued"}
    assert version.project_id == project.id
    assert version.prompt == "a box"
    assert queued == [version.id]


def test_generate_model_from_prompt_increments_version(project, monkeypatch):
    monkeypatch.setattr(render_model_stl, "delay", lambda version_id: None)

    first = call("generate_model_from_prompt", project_id=project.id, prompt="first")
    second = call("generate_model_from_prompt", project_id=project.id, prompt="second")

    assert first["version"] == 1
    assert second["version"] == 2


def test_get_model_version_returns_metadata_and_json(project):
    version = ModelVersion.objects.create(
        project=project,
        version=1,
        prompt="p",
        specification_json={"object": "box"},
        validation_json={"status": "done", "errors": []},
    )

    result = call("get_model_version", version_id=version.id)

    assert result["version_id"] == version.id
    assert result["project_id"] == project.id
    assert result["version"] == 1
    assert result["status"] == "done"
    assert result["specification_json"] == {"object": "box"}
    assert result["validation_json"] == {"status": "done", "errors": []}


def test_export_model_stl_reports_storage(project, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    relative_path = f"projects/{project.id}/v1/model.stl"
    payload = b"solid fake\nendsolid fake\n"
    LocalStorage().write_bytes(relative_path, payload)
    version = ModelVersion.objects.create(project=project, version=1)
    version.stl_file.name = relative_path
    version.save(update_fields=["stl_file"])

    result = call("export_model_stl", version_id=version.id)

    assert result == {"path": relative_path, "exists": True, "size": len(payload)}


def test_export_model_stl_missing_artifact(project, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    version = ModelVersion.objects.create(project=project, version=1)

    result = call("export_model_stl", version_id=version.id)

    assert result == {"path": None, "exists": False, "size": 0}
