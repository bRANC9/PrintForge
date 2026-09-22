"""Tests for the community / build-plate MCP tools.

Run explicitly with ``uv run pytest mcp/tests``: the backend ``testpaths``
setting only collects the top-level ``../tests`` directory.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied

from designs.models import ModelVersion
from mcp.tools import all_tools, call
from printers.models import Printer, PrintJobStatus
from projects.services import create_project
from workspaces.models import WorkspaceRole
from workspaces.services import add_member, create_workspace

User = get_user_model()

COMMUNITY_TOOLS = {
    "publish_project",
    "unpublish_project",
    "search_public_projects",
    "set_project_tags",
    "rate_project",
    "record_download",
    "generate_project_description",
    "create_build_plate",
    "add_plate_item",
    "enqueue_print_job",
}


def test_community_tools_are_registered():
    assert COMMUNITY_TOOLS <= set(all_tools())


@pytest.fixture
def owner(db):
    return User.objects.create_user(username="owner", email="owner@example.com", password="pw")


@pytest.fixture
def member(db):
    return User.objects.create_user(username="member", email="member@example.com", password="pw")


@pytest.fixture
def viewer(db):
    return User.objects.create_user(username="viewer", email="viewer@example.com", password="pw")


@pytest.fixture
def outsider(db):
    return User.objects.create_user(
        username="outsider", email="outsider@example.com", password="pw"
    )


@pytest.fixture
def workspace(owner, member, viewer):
    workspace = create_workspace(name="Lab", owner=owner)
    add_member(workspace=workspace, user=member, role=WorkspaceRole.MEMBER)
    add_member(workspace=workspace, user=viewer, role=WorkspaceRole.VIEWER)
    return workspace


@pytest.fixture
def project(workspace, owner):
    return create_project(workspace=workspace, name="Holder", created_by=owner)


def test_publish_requires_member(project, owner, member, viewer, outsider):
    with pytest.raises(PermissionDenied):
        call("publish_project", project_id=project.id, user_id=outsider.id)
    with pytest.raises(PermissionDenied):
        call("publish_project", project_id=project.id, user_id=viewer.id)

    result = call("publish_project", project_id=project.id, user_id=member.id)

    assert result == {"id": project.id, "name": "Holder", "is_public": True}


def test_search_public_projects_is_anonymous_and_filters_public(project, owner):
    hidden = create_project(workspace=project.workspace, name="Hidden", created_by=owner)
    call("publish_project", project_id=project.id, user_id=owner.id)

    results = call("search_public_projects", q="Holder")

    assert [row["id"] for row in results] == [project.id]
    assert results[0]["rating"] == {"average": None, "count": 0}
    assert all(row["id"] != hidden.id for row in call("search_public_projects"))


def test_rate_project_returns_summary(project, member):
    result = call("rate_project", project_id=project.id, user_id=member.id, score=4)

    assert result["score"] == 4
    assert result["summary"] == {"average": 4.0, "count": 1}


def test_set_project_tags_uses_service(project, member):
    result = call("set_project_tags", project_id=project.id, user_id=member.id, tags=["box", "box"])

    assert result["tags"] == ["box"]


def test_enqueue_print_job_from_build_plate(project, owner):
    plate = call(
        "create_build_plate",
        project_id=project.id,
        user_id=owner.id,
        name="Plate A",
    )
    version = ModelVersion.objects.create(project=project, version=1)
    call(
        "add_plate_item",
        build_plate_id=plate["id"],
        user_id=owner.id,
        model_version_id=version.id,
    )
    printer = Printer.objects.create(name="K2")

    result = call(
        "enqueue_print_job",
        project_id=project.id,
        user_id=owner.id,
        printer_id=printer.id,
        build_plate_id=plate["id"],
    )

    assert result["status"] == PrintJobStatus.QUEUED
    assert result["target_kind"] == "build_plate"


def test_add_plate_item_rejects_foreign_model_version(project, owner, workspace):
    other_project = create_project(workspace=workspace, name="Other", created_by=owner)
    foreign_version = ModelVersion.objects.create(project=other_project, version=1)
    plate = call(
        "create_build_plate",
        project_id=project.id,
        user_id=owner.id,
        name="Plate B",
    )

    with pytest.raises(ValueError):
        call(
            "add_plate_item",
            build_plate_id=plate["id"],
            user_id=owner.id,
            model_version_id=foreign_version.id,
        )
