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
from django.core.exceptions import PermissionDenied
from factories import (
    ModelVersionFactory,
    PrinterFactory,
    PrinterProfileFactory,
    ProjectFactory,
    UserFactory,
    WorkspaceFactory,
)

from agents.llm import LLMError
from agents.tasks import run_agent_workflow
from designs.models import ModelVersion
from designs.services import (
    artifact_path,
    create_annotation_edit,
    create_next_version,
    start_render,
    version_status,
)
from designs.tasks import render_model_stl
from files.services import LocalStorage
from mcp.tools import all_tools, call
from printers.models import PrintJob
from printers.services import QueueError, enqueue_job, job_target_kind
from projects.models import ContentSource, ModelDownload, Project, ProjectLicense, Rating
from projects.services import (
    add_plate_item,
    create_build_plate,
    create_project,
    generate_project_description,
    mark_project_printed,
    project_rating_summary,
    projects_for_workspace,
    publish_project,
    rate_project,
    record_download,
    search_public_projects,
    set_project_tags,
    unpublish_project,
)
from slicers.models import BuildPlate
from workspaces.models import Workspace, WorkspaceRole
from workspaces.services import add_member, create_workspace

pytestmark = pytest.mark.django_db

#: Tools this module proves parity for. Keep in sync with ``mcp.tools``.
PARITY_TOOLS = {
    "create_workspace",
    "create_project",
    "list_projects",
    "generate_model_from_prompt",
    "edit_model_from_annotations",
    "get_model_version",
    "export_model_stl",
    # Phase 8 community (terv.md 25.3): same services as the JSON API.
    "publish_project",
    "unpublish_project",
    "search_public_projects",
    "set_project_tags",
    "rate_project",
    "record_download",
    "mark_project_printed",
    "generate_project_description",
    # Build plates / print queue (terv.md 28., 14.).
    "create_build_plate",
    "add_plate_item",
    "enqueue_print_job",
}


@pytest.fixture
def queued(monkeypatch):
    """Capture render jobs instead of talking to the Celery broker."""
    ids: list[int] = []
    monkeypatch.setattr(render_model_stl, "delay", ids.append)
    return ids


@pytest.fixture
def queued_edits(monkeypatch):
    """Capture annotation-edit workflows instead of talking to the broker."""
    calls: list[dict] = []
    monkeypatch.setattr(
        run_agent_workflow,
        "delay",
        lambda *args, **kwargs: calls.append(kwargs),
    )
    return calls


#: One visual prompt: a 3D point plus the natural-language instruction for it.
ANNOTATION_EDITS = [
    {
        "id": "c1",
        "kind": "point",
        "point": [35.0, 12.5, 8.0],
        "normal": [0.0, 0.0, 1.0],
        "faces": [],
        "instruction": "4 mm-es átmenő lyuk",
    }
]


def test_every_registered_tool_has_a_parity_test():
    assert set(all_tools()) <= PARITY_TOOLS, (
        "A new MCP tool was registered without a parity test in "
        "tests/integration/test_mcp_parity.py"
    )


def test_dispatcher_routes_a_tool_without_a_name_parameter():
    workspace = WorkspaceFactory()
    ProjectFactory(workspace=workspace, name="Routed")

    result = call("list_projects", workspace_id=workspace.id, user_id=workspace.owner_id)

    assert result == [{"id": workspace.projects.get().id, "name": "Routed"}]


def test_call_dispatcher_forwards_a_parameter_named_name():
    """Regression: ``call``'s tool id is positional-only, so a tool parameter
    literally named ``name`` no longer collides with it."""
    owner = UserFactory()

    result = call("create_workspace", name="Lab", owner_id=owner.id)

    assert result["name"] == "Lab"


# ---------------------------------------------------------------------------
# create_workspace
# ---------------------------------------------------------------------------


def test_create_workspace_tool_delegates_to_service():
    owner = UserFactory()

    with patch("mcp.tools_impl.create_workspace", wraps=create_workspace) as service:
        result = call("create_workspace", name="Lab", owner_id=owner.id)

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
        result = call(
            "create_project",
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

    result = call("list_projects", workspace_id=workspace.id, user_id=workspace.owner_id)
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
        result = call(
            "generate_model_from_prompt",
            project_id=project.id,
            prompt="a box",
            user_id=project.created_by_id,
        )

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
# edit_model_from_annotations
# ---------------------------------------------------------------------------


def test_edit_model_from_annotations_tool_delegates_to_service(queued_edits):
    version = ModelVersionFactory(project=ProjectFactory())
    user = _member_of(version.project)

    with patch("mcp.tools_impl.create_annotation_edit", wraps=create_annotation_edit) as service:
        result = call(
            "edit_model_from_annotations",
            base_version_id=version.id,
            prompt="A kijelölt peremre tegyél egy 4 mm-es lyukat.",
            annotations=ANNOTATION_EDITS,
            user_id=user.id,
        )

    service.assert_called_once_with(
        base_version=version,
        prompt="A kijelölt peremre tegyél egy 4 mm-es lyukat.",
        annotations=ANNOTATION_EDITS,
        created_by=user,
    )
    assert result == {
        "base_version_id": version.id,
        "project_id": version.project_id,
        "queued": True,
    }
    # The service enqueues the agent workflow; no Celery/Redis is touched.
    assert queued_edits == [
        {
            "project_id": version.project_id,
            "prompt": "A kijelölt peremre tegyél egy 4 mm-es lyukat.",
            "user_id": user.id,
            "base_version_id": version.id,
            "annotations": ANNOTATION_EDITS,
        }
    ]


def test_edit_model_from_annotations_tool_rejects_a_non_member():
    version = ModelVersionFactory()
    outsider = UserFactory()

    with pytest.raises(PermissionDenied):
        call(
            "edit_model_from_annotations",
            base_version_id=version.id,
            prompt="nope",
            annotations=ANNOTATION_EDITS,
            user_id=outsider.id,
        )


def test_edit_model_from_annotations_tool_requires_annotations(queued_edits):
    version = ModelVersionFactory()
    user = _member_of(version.project)

    with pytest.raises(ValueError):
        call(
            "edit_model_from_annotations",
            base_version_id=version.id,
            prompt="empty prompt",
            annotations=[],
            user_id=user.id,
        )

    assert queued_edits == []


# ---------------------------------------------------------------------------
# get_model_version
# ---------------------------------------------------------------------------


def test_get_model_version_tool_matches_services():
    version = ModelVersionFactory(validation_json={"status": "done", "stage": "done", "errors": []})

    with patch("mcp.tools_impl.version_status", wraps=version_status) as spy:
        result = call(
            "get_model_version", version_id=version.id, user_id=version.project.created_by_id
        )

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

    result = call("export_model_stl", version_id=version.id, user_id=version.project.created_by_id)

    direct_path = artifact_path(version, "stl")
    assert direct_path == relative
    assert result == {"path": direct_path, "exists": True, "size": len(payload)}


def test_export_model_stl_tool_matches_missing_artifact(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    version = ModelVersionFactory()

    result = call("export_model_stl", version_id=version.id, user_id=version.project.created_by_id)

    assert artifact_path(version, "stl") is None
    assert result == {"path": None, "exists": False, "size": 0}


# ---------------------------------------------------------------------------
# Community tools (terv.md Phase 8) -- spies prove the delegation, then the
# return value is checked against the direct service call.
# ---------------------------------------------------------------------------


def _member_of(project):
    """The workspace owner, i.e. a caller that satisfies the MEMBER gate."""
    return project.workspace.owner


class _MetadataProvider:
    """Structured-only fake, so the AI autofill never touches a real LLM."""

    name = "fake"

    def __init__(self, summary: str, tags: list[str]) -> None:
        self._result = {"summary": summary, "tags": tags}

    def structured(self, prompt, schema, **kwargs):
        return dict(self._result)


def test_publish_project_tool_delegates_to_service():
    project = ProjectFactory(is_public=False)
    user = _member_of(project)

    with patch("mcp.tools_impl.publish_project", wraps=publish_project) as service:
        result = call("publish_project", project_id=project.id, user_id=user.id)

    service.assert_called_once_with(project)
    project.refresh_from_db()
    assert result == {"id": project.id, "name": project.name, "is_public": True}
    assert project.is_public is True


def test_unpublish_project_tool_delegates_to_service():
    project = ProjectFactory(is_public=True)
    user = _member_of(project)

    with patch("mcp.tools_impl.unpublish_project", wraps=unpublish_project) as service:
        result = call("unpublish_project", project_id=project.id, user_id=user.id)

    service.assert_called_once_with(project)
    project.refresh_from_db()
    assert result == {"id": project.id, "name": project.name, "is_public": False}
    assert project.is_public is False


def test_search_public_projects_tool_delegates_to_service():
    match = ProjectFactory(name="Alpha Bracket", is_public=True)
    ProjectFactory(name="Beta Mount", is_public=True)
    ProjectFactory(name="Alpha Hidden", is_public=False)

    with patch("mcp.tools_impl.search_public_projects", wraps=search_public_projects) as service:
        result = call("search_public_projects", q="alpha")

    service.assert_called_once_with(q="alpha", tag_slug="", license="", ordering="newest")
    assert [row["id"] for row in result] == [match.id]
    assert result[0]["rating"] == {"average": None, "count": 0}


def test_set_project_tags_tool_delegates_to_service():
    project = ProjectFactory()
    user = _member_of(project)

    with patch("mcp.tools_impl.set_project_tags", wraps=set_project_tags) as service:
        result = call(
            "set_project_tags",
            project_id=project.id,
            user_id=user.id,
            tags=["Bracket", "Desk"],
        )

    service.assert_called_once_with(project, ["Bracket", "Desk"])
    assert result == {"id": project.id, "tags": ["Bracket", "Desk"]}


def test_rate_project_tool_delegates_to_service():
    project = ProjectFactory()
    user = _member_of(project)

    with patch("mcp.tools_impl.rate_project", wraps=rate_project) as service:
        result = call("rate_project", project_id=project.id, user_id=user.id, score=4)

    service.assert_called_once_with(project, user, 4)
    assert result == {
        "project_id": project.id,
        "score": 4,
        "summary": {"average": 4.0, "count": 1},
    }
    assert project_rating_summary(project) == {"average": 4.0, "count": 1}


def test_record_download_tool_delegates_to_service():
    project = ProjectFactory()
    version = ModelVersionFactory(project=project)
    user = _member_of(project)

    with patch("mcp.tools_impl.record_download", wraps=record_download) as service:
        result = call(
            "record_download",
            project_id=project.id,
            user_id=user.id,
            model_version_id=version.id,
            ip_hash="deadbeef",
        )

    service.assert_called_once_with(project, model_version=version, user=user, ip_hash="deadbeef")
    project.refresh_from_db()
    assert result == {
        "download_id": result["download_id"],
        "project_id": project.id,
        "counted": True,
        "download_count": 1,
    }
    logged = ModelDownload.objects.get(pk=result["download_id"])
    assert logged.project_id == project.id
    assert logged.model_version_id == version.id
    assert logged.ip_hash == "deadbeef"


def test_generate_project_description_tool_delegates_to_service(monkeypatch):
    project = ProjectFactory()
    user = _member_of(project)
    monkeypatch.setattr(
        "agents.llm.get_provider",
        lambda *args, **kwargs: _MetadataProvider("A sturdy bracket.", ["bracket"]),
    )

    with patch(
        "mcp.tools_impl.generate_project_description", wraps=generate_project_description
    ) as service:
        result = call(
            "generate_project_description",
            project_id=project.id,
            user_id=user.id,
            force=True,
        )

    service.assert_called_once_with(project, force=True)
    assert result["project_id"] == project.id
    assert result["applied"] is True
    assert result["description"] == "A sturdy bracket."
    assert result["tags"] == ["bracket"]


def test_create_build_plate_tool_delegates_to_service():
    project = ProjectFactory()
    user = _member_of(project)
    profile = PrinterProfileFactory()

    with patch("mcp.tools_impl.create_build_plate", wraps=create_build_plate) as service:
        result = call(
            "create_build_plate",
            project_id=project.id,
            user_id=user.id,
            name="Plate A",
            printer_profile_id=profile.id,
        )

    service.assert_called_once_with(
        project=project,
        name="Plate A",
        created_by=user,
        printer_profile=profile,
        settings_json=None,
    )
    plate = BuildPlate.objects.get(pk=result["id"])
    assert result == {"id": plate.id, "project_id": project.id, "name": "Plate A"}
    assert plate.project_id == project.id


def test_add_plate_item_tool_delegates_to_service():
    project = ProjectFactory()
    user = _member_of(project)
    version = ModelVersionFactory(project=project)
    plate = create_build_plate(project=project, name="Plate", created_by=user)

    with patch("mcp.tools_impl.add_plate_item", wraps=add_plate_item) as service:
        result = call(
            "add_plate_item",
            build_plate_id=plate.id,
            user_id=user.id,
            model_version_id=version.id,
            position_x=10.0,
        )

    service.assert_called_once_with(
        plate,
        model_version=version,
        position_x=10.0,
        position_y=0.0,
        position_z=0.0,
        rotation_z=0.0,
        scale=1.0,
        settings_json=None,
    )
    assert result == {
        "id": result["id"],
        "build_plate_id": plate.id,
        "model_version_id": version.id,
    }
    assert plate.items.get(pk=result["id"]).position_x == 10.0


def test_enqueue_print_job_tool_delegates_to_service():
    project = ProjectFactory()
    user = _member_of(project)
    version = ModelVersionFactory(project=project)
    physical = PrinterFactory()

    with patch("mcp.tools_impl.enqueue_job", wraps=enqueue_job) as service:
        result = call(
            "enqueue_print_job",
            project_id=project.id,
            user_id=user.id,
            printer_id=physical.id,
            model_version_id=version.id,
            priority=3,
        )

    service.assert_called_once_with(
        project=project,
        printer=physical,
        model_version=version,
        build_plate=None,
        created_by=user,
        priority=3,
    )
    job = Project.objects.get(pk=project.id).print_jobs.get()
    assert result == {"job_id": job.id, "status": "QUEUED", "target_kind": "model_version"}
    assert job_target_kind(job) == "model_version"


# ---------------------------------------------------------------------------
# Community / build-plate semantics (terv.md Phase 8, 28.)
# ---------------------------------------------------------------------------


def test_rate_project_tool_updates_instead_of_duplicating():
    project = ProjectFactory()
    user = _member_of(project)

    first = call("rate_project", project_id=project.id, user_id=user.id, score=5)
    second = call("rate_project", project_id=project.id, user_id=user.id, score=2)

    assert first["score"] == 5
    assert second["score"] == 2
    assert second["summary"] == {"average": 2.0, "count": 1}
    assert Rating.objects.filter(project=project, user=user).count() == 1


def test_rate_project_tool_averages_across_users():
    project = ProjectFactory()
    member = _member_of(project)
    other = UserFactory()
    add_member(workspace=project.workspace, user=other, role=WorkspaceRole.MEMBER)

    call("rate_project", project_id=project.id, user_id=member.id, score=5)
    result = call("rate_project", project_id=project.id, user_id=other.id, score=1)

    assert result["summary"] == {"average": 3.0, "count": 2}


def test_rate_project_tool_rejects_an_out_of_range_score():
    project = ProjectFactory()

    with pytest.raises(ValueError, match="between 1 and 5"):
        call("rate_project", project_id=project.id, user_id=_member_of(project).id, score=6)


def test_record_download_tool_increments_the_counter():
    project = ProjectFactory()
    first_user = _member_of(project)
    second_user = UserFactory()
    add_member(workspace=project.workspace, user=second_user, role=WorkspaceRole.MEMBER)

    first = call("record_download", project_id=project.id, user_id=first_user.id)
    second = call("record_download", project_id=project.id, user_id=second_user.id)

    project.refresh_from_db()
    assert first["download_count"] == 1
    assert second["download_count"] == 2
    assert project.download_count == 2
    assert ModelDownload.objects.filter(project=project).count() == 2


def test_record_download_tool_dedupes_the_same_user():
    project = ProjectFactory()
    user = _member_of(project)

    first = call("record_download", project_id=project.id, user_id=user.id)
    second = call("record_download", project_id=project.id, user_id=user.id)

    project.refresh_from_db()
    assert first["counted"] is True
    assert second["counted"] is False
    assert project.download_count == 1
    assert ModelDownload.objects.filter(project=project).count() == 1


def test_mark_project_printed_tool_delegates_to_service():
    project = ProjectFactory()
    user = _member_of(project)

    with patch("mcp.tools_impl.mark_project_printed", wraps=mark_project_printed) as service:
        result = call("mark_project_printed", project_id=project.id, user_id=user.id)

    service.assert_called_once_with(project, user=user)
    project.refresh_from_db()
    assert result == {"project_id": project.id, "counted": True, "print_count": 1}
    assert project.print_count == 1


def test_record_download_tool_without_a_version_logs_none():
    project = ProjectFactory()
    user = _member_of(project)

    result = call("record_download", project_id=project.id, user_id=user.id)

    logged = ModelDownload.objects.get(pk=result["download_id"])
    assert logged.model_version_id is None
    assert logged.user_id == user.id


def test_record_download_tool_rejects_a_version_from_another_project():
    project = ProjectFactory()
    other_version = ModelVersionFactory(project=ProjectFactory())

    with pytest.raises(ValueError, match="does not belong"):
        call(
            "record_download",
            project_id=project.id,
            user_id=_member_of(project).id,
            model_version_id=other_version.id,
        )


def test_search_public_projects_tool_filters_by_tag_and_license():
    tagged = ProjectFactory(name="Tagged", is_public=True, license=ProjectLicense.MIT)
    set_project_tags(tagged, ["bracket"])
    other = ProjectFactory(name="Other", is_public=True, license=ProjectLicense.CC0)

    by_tag = call("search_public_projects", tag_slug="bracket")
    by_license = call("search_public_projects", license=ProjectLicense.CC0)

    assert [row["id"] for row in by_tag] == [tagged.id]
    assert by_tag[0]["tags"] == ["bracket"]
    assert [row["id"] for row in by_license] == [other.id]


def test_search_public_projects_tool_orders_by_downloads():
    low = ProjectFactory(name="Low", is_public=True, download_count=1)
    high = ProjectFactory(name="High", is_public=True, download_count=5)

    result = call("search_public_projects", ordering="downloads")

    assert [row["id"] for row in result] == [high.id, low.id]


def test_search_public_projects_tool_clamps_the_limit():
    ProjectFactory.create_batch(3, is_public=True)

    assert len(call("search_public_projects", limit=2)) == 2
    assert len(call("search_public_projects", limit=0)) == 1
    assert len(call("search_public_projects", limit=10_000)) == 3


def test_publish_then_unpublish_changes_search_visibility():
    project = ProjectFactory(is_public=False)
    user = _member_of(project)

    assert call("search_public_projects", q=project.name) == []

    call("publish_project", project_id=project.id, user_id=user.id)
    assert [row["id"] for row in call("search_public_projects", q=project.name)] == [project.id]

    call("unpublish_project", project_id=project.id, user_id=user.id)
    assert call("search_public_projects", q=project.name) == []


def test_publish_and_unpublish_are_idempotent():
    project = ProjectFactory(is_public=False)
    user = _member_of(project)

    assert call("publish_project", project_id=project.id, user_id=user.id)["is_public"] is True
    assert call("publish_project", project_id=project.id, user_id=user.id)["is_public"] is True
    assert Project.objects.get(pk=project.id).is_public is True

    assert call("unpublish_project", project_id=project.id, user_id=user.id)["is_public"] is False
    assert call("unpublish_project", project_id=project.id, user_id=user.id)["is_public"] is False
    assert Project.objects.get(pk=project.id).is_public is False


def test_set_project_tags_tool_replaces_existing_tags():
    project = ProjectFactory()
    user = _member_of(project)

    call("set_project_tags", project_id=project.id, user_id=user.id, tags=["old"])
    result = call(
        "set_project_tags",
        project_id=project.id,
        user_id=user.id,
        tags=["new", "other"],
    )

    assert result == {"id": project.id, "tags": ["new", "other"]}
    assert set(project.tags.values_list("name", flat=True)) == {"new", "other"}
    project.refresh_from_db()
    assert project.tags_source == ContentSource.MANUAL


def test_create_build_plate_tool_without_a_profile():
    project = ProjectFactory()

    result = call(
        "create_build_plate",
        project_id=project.id,
        user_id=_member_of(project).id,
        name="Plain plate",
    )

    plate = BuildPlate.objects.get(pk=result["id"])
    assert plate.printer_profile_id is None
    assert plate.settings_json == {}


def test_add_plate_item_tool_rejects_a_version_from_another_project():
    project = ProjectFactory()
    user = _member_of(project)
    plate = create_build_plate(project=project, name="Plate", created_by=user)
    other_version = ModelVersionFactory(project=ProjectFactory())

    with pytest.raises(ValueError, match="does not belong"):
        call(
            "add_plate_item",
            build_plate_id=plate.id,
            user_id=user.id,
            model_version_id=other_version.id,
        )


def test_enqueue_print_job_tool_delegates_with_a_build_plate():
    project = ProjectFactory()
    user = _member_of(project)
    plate = create_build_plate(project=project, name="Plate", created_by=user)
    physical = PrinterFactory()

    with patch("mcp.tools_impl.enqueue_job", wraps=enqueue_job) as service:
        result = call(
            "enqueue_print_job",
            project_id=project.id,
            user_id=user.id,
            printer_id=physical.id,
            build_plate_id=plate.id,
        )

    service.assert_called_once_with(
        project=project,
        printer=physical,
        model_version=None,
        build_plate=plate,
        created_by=user,
        priority=0,
    )
    assert result["target_kind"] == "build_plate"
    job = PrintJob.objects.get(pk=result["job_id"])
    assert job.build_plate_id == plate.id
    assert job.model_version_id is None


def test_enqueue_print_job_tool_requires_a_target():
    project = ProjectFactory()
    user = _member_of(project)

    with pytest.raises(QueueError, match="model_version or a build_plate"):
        call(
            "enqueue_print_job",
            project_id=project.id,
            user_id=user.id,
            printer_id=PrinterFactory().id,
        )


# ---------------------------------------------------------------------------
# Permission gates (same workspace-role seam as the DRF API)
# ---------------------------------------------------------------------------


def test_community_write_tools_reject_a_non_member():
    project = ProjectFactory()
    outsider = UserFactory()

    for tool_name, kwargs in [
        ("publish_project", {"project_id": project.id, "user_id": outsider.id}),
        ("unpublish_project", {"project_id": project.id, "user_id": outsider.id}),
        ("set_project_tags", {"project_id": project.id, "user_id": outsider.id, "tags": []}),
        ("rate_project", {"project_id": project.id, "user_id": outsider.id, "score": 5}),
    ]:
        with pytest.raises(PermissionDenied):
            call(tool_name, **kwargs)


def test_record_download_tool_rejects_a_non_member():
    project = ProjectFactory()

    with pytest.raises(PermissionDenied):
        call("record_download", project_id=project.id, user_id=UserFactory().id)


def test_add_plate_item_tool_rejects_a_non_member():
    project = ProjectFactory()
    user = _member_of(project)
    plate = create_build_plate(project=project, name="Plate", created_by=user)
    version = ModelVersionFactory(project=project)

    with pytest.raises(PermissionDenied):
        call(
            "add_plate_item",
            build_plate_id=plate.id,
            user_id=UserFactory().id,
            model_version_id=version.id,
        )


# ---------------------------------------------------------------------------
# AI description / tags -- documented behaviour on LLM failure and provenance
# ---------------------------------------------------------------------------


class _BoomProvider:
    name = "boom"

    def structured(self, prompt, schema, **kwargs):
        raise LLMError("no model available")


def test_generate_project_description_tool_reports_llm_errors(monkeypatch):
    project = ProjectFactory()
    user = _member_of(project)
    monkeypatch.setattr("agents.llm.get_provider", lambda *args, **kwargs: _BoomProvider())

    result = call("generate_project_description", project_id=project.id, user_id=user.id)

    assert result["project_id"] == project.id
    assert result["applied"] is False
    assert result["reason"] == "llm_error"
    assert "no model available" in result["error"]


def test_generate_project_description_tool_respects_manual_provenance():
    project = ProjectFactory(description="Hand written")
    project.description_source = ContentSource.MANUAL
    project.tags_source = ContentSource.MANUAL
    project.save(update_fields=["description_source", "tags_source"])

    result = call(
        "generate_project_description",
        project_id=project.id,
        user_id=_member_of(project).id,
    )

    assert result["applied"] is False
    assert result["reason"] == "manual"
