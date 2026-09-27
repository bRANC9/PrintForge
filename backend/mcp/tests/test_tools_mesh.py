"""MCP parity tests for the mesh-import tools.

Same shape as ``tests/integration/test_mcp_parity.py``: the delegation itself is
proven by spying on the ``services.py`` symbol imported by ``mcp.tools_impl``,
and the tool's return value is then checked against the direct service call. The
three tools are the MCP twin of ``POST /projects/{id}/versions/from-mesh/``,
``POST /versions/{id}/source-mesh/`` and ``GET /versions/{id}/source-mesh/``.

Run with ``uv run pytest mcp/tests/test_tools_mesh.py``.
"""

from __future__ import annotations

import base64
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied

from designs.models import ModelVersion
from designs.services import (
    MESH_SOURCE_FORMATS,
    attach_source_mesh_to_version,
    create_next_version,
    create_next_version_from_mesh,
    source_mesh_path,
    start_render,
)
from designs.tasks import render_model_stl
from files.services import LocalStorage
from mcp.tools import all_tools, call
from projects.services import create_project
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()

MESH_TOOLS = {
    "create_version_from_mesh",
    "attach_source_mesh_to_version",
    "get_source_mesh",
}


def box_stl() -> bytes:
    """A watertight 10x20x30 mm box as binary STL bytes."""
    import trimesh

    return bytes(trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="stl"))


def open_box_stl() -> bytes:
    """A box with one triangle removed: parses, but is not watertight."""
    import trimesh

    box = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
    holed = trimesh.Trimesh(vertices=box.vertices.copy(), faces=box.faces[:-1], process=False)
    return bytes(holed.export(file_type="stl"))


def b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


@pytest.fixture
def project(db):
    user = User.objects.create_user(username="ada", email="ada@example.com", password="pw")
    workspace = create_workspace(name="Lab", owner=user)
    return create_project(workspace=workspace, name="Holder", created_by=user)


@pytest.fixture
def media(settings, tmp_path) -> LocalStorage:
    settings.MEDIA_ROOT = str(tmp_path)
    return LocalStorage(root=tmp_path)


@pytest.fixture
def queued(monkeypatch):
    """Capture render jobs instead of talking to the Celery broker."""
    ids: list[int] = []
    monkeypatch.setattr(render_model_stl, "delay", ids.append)
    return ids


def test_mesh_tools_are_registered():
    assert MESH_TOOLS <= set(all_tools())


# ---------------------------------------------------------------------------
# create_version_from_mesh
# ---------------------------------------------------------------------------


def test_create_version_from_mesh_tool_delegates_to_service(project, media, queued):
    payload = box_stl()

    with (
        patch(
            "mcp.tools_impl.create_next_version_from_mesh",
            wraps=create_next_version_from_mesh,
        ) as create,
        patch("mcp.tools_impl.start_render", wraps=start_render) as render,
    ):
        result = call(
            "create_version_from_mesh",
            project_id=project.id,
            user_id=project.created_by_id,
            filename="generated.stl",
            mesh_base64=b64(payload),
            scale_mm=42.0,
        )

    create.assert_called_once_with(
        project=project,
        mesh_bytes=payload,
        filename="generated.stl",
        created_by=project.created_by,
        scale_mm=42.0,
        rotate_deg=None,
        repair=None,
        reference_note="",
    )
    version = ModelVersion.objects.get(pk=result["version_id"])
    render.assert_called_once_with(version)
    assert result == {
        "version_id": version.id,
        "project_id": project.id,
        "version": 1,
        "status": "queued",
        "source_mesh": source_mesh_path(version),
        "warnings": [],
    }
    assert version.specification_json["generator"] == "mesh"
    assert media.exists(version.source_mesh.name)
    assert queued == [version.id]


def test_create_version_from_mesh_tool_rejects_a_non_member(project, media, queued):
    outsider = User.objects.create_user(username="eve", email="eve@example.com", password="pw")

    with pytest.raises(PermissionDenied):
        call(
            "create_version_from_mesh",
            project_id=project.id,
            user_id=outsider.id,
            filename="box.stl",
            mesh_base64=b64(box_stl()),
        )

    assert not ModelVersion.objects.exists()
    assert queued == []


def test_create_version_from_mesh_tool_rejects_a_mislabelled_payload(project, media, queued):
    with pytest.raises(ValueError, match="not printable|could not parse|no geometry"):
        call(
            "create_version_from_mesh",
            project_id=project.id,
            user_id=project.created_by_id,
            filename="broken.stl",
            mesh_base64=b64(open_box_stl()),
        )

    assert not ModelVersion.objects.exists()
    assert queued == []


def test_create_version_from_mesh_tool_rejects_an_empty_payload(project, media, queued):
    """``mesh_base64=""`` decodes to zero bytes, which the *service* refuses.

    ``base64.b64decode("")`` is a valid empty decode rather than a transport
    error, so a zero-byte mesh is a plausible client mistake that lands on the same
    ``ValueError`` -- and therefore the same English message -- as the HTTP
    endpoints' ``{"detail": ...}``. Nothing is written or queued.
    """
    with pytest.raises(ValueError, match="empty") as excinfo:
        call(
            "create_version_from_mesh",
            project_id=project.id,
            user_id=project.created_by_id,
            filename="box.stl",
            mesh_base64="",
        )

    assert ", ".join(MESH_SOURCE_FORMATS) in str(excinfo.value)
    assert not ModelVersion.objects.exists()
    assert not (media.root / "projects" / str(project.pk)).exists()
    assert queued == []


def test_create_version_from_mesh_tool_rejects_a_name_without_a_suffix(project, media, queued):
    """A filename is only read for its suffix; the rest of it never becomes a path.

    Nothing sanitises the name on this transport, so it reaches the service as
    given: a name with no extension is refused with the readable message, while a
    directory-prefixed one is accepted on its suffix and stored under the
    server-generated per-version name.
    """
    for filename in ("mesh", ""):
        with pytest.raises(ValueError) as excinfo:
            call(
                "create_version_from_mesh",
                project_id=project.id,
                user_id=project.created_by_id,
                filename=filename,
                mesh_base64=b64(box_stl()),
            )
        assert "Unsupported source mesh" in str(excinfo.value)
        assert ", ".join(MESH_SOURCE_FORMATS) in str(excinfo.value)

    result = call(
        "create_version_from_mesh",
        project_id=project.id,
        user_id=project.created_by_id,
        filename="../../escaped.stl",
        mesh_base64=b64(box_stl()),
    )

    assert result["source_mesh"] == f"projects/{project.pk}/v1/source.stl"
    assert [p.name for p in media.root.rglob("escaped.stl")] == []
    assert ModelVersion.objects.count() == 1
    assert queued == [result["version_id"]]


def test_create_version_from_mesh_tool_rejects_invalid_base64(project, media, queued):
    with pytest.raises(ValueError, match="base64"):
        call(
            "create_version_from_mesh",
            project_id=project.id,
            user_id=project.created_by_id,
            filename="box.stl",
            mesh_base64="not base64!!",
        )

    assert not ModelVersion.objects.exists()
    assert queued == []


# ---------------------------------------------------------------------------
# attach_source_mesh_to_version
# ---------------------------------------------------------------------------


def test_attach_source_mesh_to_version_tool_delegates_to_service(project, media, queued):
    version = create_next_version(project=project, prompt="v1")
    payload = box_stl()

    with (
        patch(
            "mcp.tools_impl.attach_source_mesh_to_version",
            wraps=attach_source_mesh_to_version,
        ) as service,
        patch("mcp.tools_impl.start_render", wraps=start_render) as render,
    ):
        result = call(
            "attach_source_mesh_to_version",
            version_id=version.id,
            user_id=project.created_by_id,
            filename="box.stl",
            mesh_base64=b64(payload),
            rotate_deg=[0.0, 0.0, 90.0],
            repair=True,
        )

    service.assert_called_once_with(
        version=version,
        mesh_bytes=payload,
        filename="box.stl",
        scale_mm=None,
        rotate_deg=[0.0, 0.0, 90.0],
        repair=True,
        reference_note="",
    )
    version.refresh_from_db()
    render.assert_called_once_with(version)
    assert result["version_id"] == version.id
    assert result["source_mesh"] == f"projects/{project.pk}/v1/source.stl"
    assert version.specification_json["transform"] == {"rotate_deg": [0.0, 0.0, 90.0]}
    assert version.specification_json["repair"] == {"enabled": True}
    assert queued == [version.id]


def test_attach_source_mesh_to_version_tool_rejects_an_empty_payload(project, media, queued):
    """Same empty-mesh message as the create tool, and the old mesh is untouched."""
    version = create_next_version_from_mesh(
        project=project, mesh_bytes=box_stl(), filename="box.stl"
    )
    before = version.source_mesh.name

    with pytest.raises(ValueError, match="empty"):
        call(
            "attach_source_mesh_to_version",
            version_id=version.id,
            user_id=project.created_by_id,
            filename="box.stl",
            mesh_base64="",
        )

    version.refresh_from_db()
    assert version.source_mesh.name == before
    assert media.read_bytes(before) == box_stl()
    assert queued == []


def test_attach_source_mesh_to_version_tool_rejects_a_non_member(project, media, queued):
    version = create_next_version(project=project, prompt="v1")
    outsider = User.objects.create_user(username="eve", email="eve@example.com", password="pw")

    with pytest.raises(PermissionDenied):
        call(
            "attach_source_mesh_to_version",
            version_id=version.id,
            user_id=outsider.id,
            filename="box.stl",
            mesh_base64=b64(box_stl()),
        )

    version.refresh_from_db()
    assert not version.source_mesh.name
    assert queued == []


# ---------------------------------------------------------------------------
# get_source_mesh
# ---------------------------------------------------------------------------


def test_get_source_mesh_tool_matches_the_storage_reader(project, media):
    version = create_next_version_from_mesh(
        project=project, mesh_bytes=box_stl(), filename="box.stl"
    )
    payload = box_stl()

    result = call("get_source_mesh", version_id=version.id, user_id=project.created_by_id)

    assert result == {
        "path": source_mesh_path(version),
        "exists": True,
        "size": len(payload),
    }


def test_get_source_mesh_tool_matches_a_version_without_a_mesh(project, media):
    version = create_next_version(project=project, prompt="v1")

    result = call("get_source_mesh", version_id=version.id, user_id=project.created_by_id)

    assert result == {"path": None, "exists": False, "size": 0}


def test_get_source_mesh_tool_rejects_a_non_member(project, media):
    version = create_next_version_from_mesh(
        project=project, mesh_bytes=box_stl(), filename="box.stl"
    )
    outsider = User.objects.create_user(username="eve", email="eve@example.com", password="pw")

    with pytest.raises(PermissionDenied):
        call("get_source_mesh", version_id=version.id, user_id=outsider.id)
