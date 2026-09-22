"""Workspace-role gating for the MCP tools (terv.md 20., 25.3).

``mcp.tools.call`` runs each tool's ``authorize`` gate before the handler, and
that gate delegates to the same ``api.permissions`` matrix the HTTP API uses.
These tests pin the MEMBER (write) vs VIEWER (read/record) split, the
non-member denial, and that public search needs no identity at all.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import PermissionDenied
from factories import ModelVersionFactory, PrinterFactory, ProjectFactory, UserFactory

from mcp.tools import call
from slicers.models import BuildPlate
from workspaces.models import WorkspaceRole
from workspaces.services import add_member

pytestmark = pytest.mark.django_db


@pytest.fixture
def project():
    return ProjectFactory()


@pytest.fixture
def member(project):
    user = UserFactory()
    add_member(workspace=project.workspace, user=user, role=WorkspaceRole.MEMBER)
    return user


@pytest.fixture
def viewer(project):
    user = UserFactory()
    add_member(workspace=project.workspace, user=user, role=WorkspaceRole.VIEWER)
    return user


def test_public_search_requires_no_identity(project):
    project.is_public = True
    project.save(update_fields=["is_public"])
    ProjectFactory(name="Hidden", is_public=False)

    results = call("search_public_projects")

    assert [row["id"] for row in results] == [project.id]


def test_viewer_may_record_a_download(project, viewer):
    result = call("record_download", project_id=project.id, user_id=viewer.id)

    project.refresh_from_db()
    assert result["project_id"] == project.id
    assert result["download_count"] == 1


def test_viewer_may_read_projects_and_versions(project, viewer):
    version = ModelVersionFactory(project=project)

    listed = call("list_projects", workspace_id=project.workspace_id, user_id=viewer.id)
    assert {"id": project.id, "name": project.name} in listed

    detail = call("get_model_version", version_id=version.id, user_id=viewer.id)
    assert detail["version_id"] == version.id

    exported = call("export_model_stl", version_id=version.id, user_id=viewer.id)
    assert exported == {"path": None, "exists": False, "size": 0}


def test_viewer_is_denied_every_member_tool(project, viewer):
    version = ModelVersionFactory(project=project)
    printer = PrinterFactory()
    plate = BuildPlate.objects.create(project=project, name="Viewer plate")
    denied = [
        ("publish_project", {"project_id": project.id, "user_id": viewer.id}),
        (
            "set_project_tags",
            {"project_id": project.id, "user_id": viewer.id, "tags": ["x"]},
        ),
        ("rate_project", {"project_id": project.id, "user_id": viewer.id, "score": 5}),
        (
            "generate_project_description",
            {"project_id": project.id, "user_id": viewer.id},
        ),
        (
            "create_project",
            {"workspace_id": project.workspace_id, "user_id": viewer.id, "name": "Nope"},
        ),
        (
            "generate_model_from_prompt",
            {"project_id": project.id, "user_id": viewer.id, "prompt": "x"},
        ),
        (
            "create_build_plate",
            {"project_id": project.id, "user_id": viewer.id, "name": "Nope"},
        ),
        (
            "enqueue_print_job",
            {
                "project_id": project.id,
                "user_id": viewer.id,
                "printer_id": printer.id,
                "model_version_id": version.id,
            },
        ),
        (
            "add_plate_item",
            {
                "build_plate_id": plate.id,
                "user_id": viewer.id,
                "model_version_id": version.id,
            },
        ),
    ]

    for tool, kwargs in denied:
        with pytest.raises(PermissionDenied):
            call(tool, **kwargs)


def test_non_member_is_denied_on_project_tools(project):
    outsider = UserFactory()
    version = ModelVersionFactory(project=project)

    denied = [
        ("publish_project", {"project_id": project.id, "user_id": outsider.id}),
        ("record_download", {"project_id": project.id, "user_id": outsider.id}),
        ("rate_project", {"project_id": project.id, "user_id": outsider.id, "score": 5}),
        ("list_projects", {"workspace_id": project.workspace_id, "user_id": outsider.id}),
        (
            "create_project",
            {"workspace_id": project.workspace_id, "user_id": outsider.id, "name": "Nope"},
        ),
        (
            "generate_model_from_prompt",
            {"project_id": project.id, "user_id": outsider.id, "prompt": "x"},
        ),
        ("get_model_version", {"version_id": version.id, "user_id": outsider.id}),
        ("export_model_stl", {"version_id": version.id, "user_id": outsider.id}),
    ]

    for tool, kwargs in denied:
        with pytest.raises(PermissionDenied):
            call(tool, **kwargs)


def test_member_can_write_community_metadata(project, member):
    published = call("publish_project", project_id=project.id, user_id=member.id)
    assert published["is_public"] is True

    tagged = call("set_project_tags", project_id=project.id, user_id=member.id, tags=["Box"])
    assert tagged["tags"] == ["Box"]

    rated = call("rate_project", project_id=project.id, user_id=member.id, score=5)
    assert rated["summary"] == {"average": 5.0, "count": 1}

    created = call(
        "create_project",
        workspace_id=project.workspace_id,
        user_id=member.id,
        name="Made by member",
    )
    assert created["name"] == "Made by member"
