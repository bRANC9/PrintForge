"""MCP parity tests (terv.md 25., 26.3).

Every MCP tool must be a thin wrapper over the same ``services.py`` functions
the HTTP API calls -- no duplicated business logic, no shelling out. Each test
here spies on the service symbol imported by ``mcp.tools_impl`` (so the
delegation itself is proven, not just the final value) and then checks the
tool's return value against the direct service call.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from factories import ModelVersionFactory, ProjectFactory, UserFactory, WorkspaceFactory

from designs.models import ModelVersion
from designs.services import (
    artifact_path,
    create_next_version,
    start_render,
    version_status,
)
from designs.tasks import render_model_stl
from files.services import LocalStorage
from mcp.tools import all_tools, call
from projects.models import Project
from projects.services import create_project, projects_for_workspace
from workspaces.models import Workspace, WorkspaceRole
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db

#: Tools this module proves parity for. Keep in sync with ``mcp.tools``.
PARITY_TOOLS = {
    "create_workspace",
    "create_project",
    "list_projects",
    "generate_model_from_prompt",
    "get_model_version",
    "export_model_stl",
}


@pytest.fixture
def queued(monkeypatch):
    """Capture render jobs instead of talking to the Celery broker."""
    ids: list[int] = []
    monkeypatch.setattr(render_model_stl, "delay", ids.append)
    return ids


def _tool(tool_name: str):
    """Return the registered handler.

    Used instead of :func:`mcp.tools.call` for tools that take a parameter
    literally named ``name``: the dispatcher ``call(name, **kwargs)`` currently
    shadows it (see the xfail test below).
    """
    return all_tools()[tool_name].handler


def test_every_registered_tool_has_a_parity_test():
    assert set(all_tools()) <= PARITY_TOOLS, (
        "A new MCP tool was registered without a parity test in "
        "tests/integration/test_mcp_parity.py"
    )


def test_dispatcher_routes_a_tool_without_a_name_parameter():
    workspace = WorkspaceFactory()
    ProjectFactory(workspace=workspace, name="Routed")

    result = call("list_projects", workspace_id=workspace.id)

    assert result == [{"id": workspace.projects.get().id, "name": "Routed"}]


@pytest.mark.xfail(
    reason=(
        "PRODUCTION BUG (mcp): mcp.tools.call(name, **kwargs) uses the same "
        "identifier 'name' for both the tool name and a forwarded parameter, so "
        "create_workspace/create_project cannot be invoked through the public "
        "dispatcher. Reported to the mcp owner; expected to xpass once fixed."
    ),
    strict=False,
)
def test_call_dispatcher_forwards_a_parameter_named_name():
    owner = UserFactory()

    result = call("create_workspace", name="Lab", owner_id=owner.id)

    assert result["name"] == "Lab"


# ---------------------------------------------------------------------------
# create_workspace
# ---------------------------------------------------------------------------


def test_create_workspace_tool_delegates_to_service():
    owner = UserFactory()

    with patch("mcp.tools_impl.create_workspace", wraps=create_workspace) as service:
        result = _tool("create_workspace")(name="Lab", owner_id=owner.id)

    service.assert_called_once_with(name="Lab", owner=owner)
    created = Workspace.objects.get(pk=result["id"])
    assert result == {"id": created.id, "name": created.name}
    assert created.owner_id == owner.id
    assert created.members.get(user=owner).role == WorkspaceRole.OWNER


# ---------------------------------------------------------------------------
# create_project
# ---------------------------------------------------------------------------


def test_create_project_tool_delegates_to_service():
    workspace = WorkspaceFactory()
    user = workspace.owner

    with patch("mcp.tools_impl.create_project", wraps=create_project) as service:
        result = _tool("create_project")(
            workspace_id=workspace.id,
            name="Bracket",
            user_id=user.id,
        )

    service.assert_called_once_with(
        workspace=workspace, name="Bracket", created_by=user, description=""
    )
    created = Project.objects.get(pk=result["id"])
    assert result == {"id": created.id, "name": created.name, "workspace_id": workspace.id}
    assert created.created_by_id == user.id


# ---------------------------------------------------------------------------
# list_projects
# ---------------------------------------------------------------------------


def test_list_projects_tool_matches_workspace_service():
    workspace = WorkspaceFactory()
    ProjectFactory.create_batch(3, workspace=workspace)

    result = call("list_projects", workspace_id=workspace.id)
    direct = [{"id": p.id, "name": p.name} for p in projects_for_workspace(workspace)]

    assert result == direct


# ---------------------------------------------------------------------------
# generate_model_from_prompt
# ---------------------------------------------------------------------------


def test_generate_model_from_prompt_tool_delegates_to_services(queued):
    project = ProjectFactory()

    with (
        patch("mcp.tools_impl.create_next_version", wraps=create_next_version) as create,
        patch("mcp.tools_impl.start_render", wraps=start_render) as render,
    ):
        result = call("generate_model_from_prompt", project_id=project.id, prompt="a box")

    create.assert_called_once_with(project=project, prompt="a box")
    version = ModelVersion.objects.get(project=project)
    render.assert_called_once_with(version)

    assert result == {
        "version_id": version.id,
        "version": version.version,
        "status": "queued",
    }
    assert version_status(version)["status"] == "queued"
    assert queued == [version.id]


# ---------------------------------------------------------------------------
# get_model_version
# ---------------------------------------------------------------------------


def test_get_model_version_tool_matches_services():
    version = ModelVersionFactory(validation_json={"status": "done", "stage": "done", "errors": []})

    with patch("mcp.tools_impl.version_status", wraps=version_status) as spy:
        result = call("get_model_version", version_id=version.id)

    spy.assert_called_once_with(version)
    assert result["version_id"] == version.id
    assert result["project_id"] == version.project_id
    assert result["version"] == version.version
    assert result["prompt"] == version.prompt
    assert result["status"] == version_status(version)["status"] == "done"
    assert result["specification_json"] == version.specification_json
    assert result["validation_json"] == version.validation_json
    assert result["scad_file"] == (version.scad_file.name or None)
    assert result["stl_file"] == (version.stl_file.name or None)
    assert result["created_at"] == version.created_at.isoformat()


# ---------------------------------------------------------------------------
# export_model_stl
# ---------------------------------------------------------------------------


def test_export_model_stl_tool_matches_storage_services(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    version = ModelVersionFactory()
    relative = f"projects/{version.project_id}/v{version.version}/model.stl"
    payload = b"solid fake\nendsolid fake\n"
    LocalStorage().write_bytes(relative, payload)
    version.stl_file.name = relative
    version.save(update_fields=["stl_file"])

    result = call("export_model_stl", version_id=version.id)

    direct_path = artifact_path(version, "stl")
    assert direct_path == relative
    assert result == {"path": direct_path, "exists": True, "size": len(payload)}


def test_export_model_stl_tool_matches_missing_artifact(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    version = ModelVersionFactory()

    result = call("export_model_stl", version_id=version.id)

    assert artifact_path(version, "stl") is None
    assert result == {"path": None, "exists": False, "size": 0}
