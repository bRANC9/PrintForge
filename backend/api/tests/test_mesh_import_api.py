"""Endpoint tests for the mesh-import JSON API contract.

Both upload endpoints are multipart and share the same rules: a printable mesh is
accepted (``201``/``200`` + the standard version payload), anything else is a
``400`` that says why -- the user learns at upload time that the mesh cannot be
printed instead of watching a failed render job later.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APIClient

from designs.models import ModelVersion
from designs.services import MESH_SOURCE_FORMATS, create_next_version, create_next_version_from_mesh
from designs.tasks import render_model_stl
from files.services import LocalStorage
from projects.services import create_project
from workspaces.models import WorkspaceRole
from workspaces.services import add_member, create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()

FROM_MESH_URL = "/api/v1/projects/{pk}/versions/from-mesh/"


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


def upload(name: str, payload: bytes) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, payload, content_type="application/octet-stream")


@pytest.fixture(autouse=True)
def media(settings, tmp_path) -> LocalStorage:
    settings.MEDIA_ROOT = str(tmp_path)
    return LocalStorage(root=tmp_path)


@pytest.fixture(autouse=True)
def no_broker(monkeypatch):
    """Never talk to a real Celery broker during these tests."""
    queued: list[int] = []
    monkeypatch.setattr(render_model_stl, "delay", queued.append)
    return queued


@pytest.fixture
def user():
    return User.objects.create_user(username="ada", email="ada@example.com", password="pw")


@pytest.fixture
def client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def project(user):
    workspace = create_workspace(name="Lab", owner=user)
    return create_project(workspace=workspace, name="Holder", created_by=user)


@pytest.fixture
def viewer(project):
    user = User.objects.create_user(username="vic", email="vic@example.com", password="pw")
    add_member(workspace=project.workspace, user=user, role=WorkspaceRole.VIEWER)
    return user


# ---------------------------------------------------------------------------
# POST /api/v1/projects/{id}/versions/from-mesh/
# ---------------------------------------------------------------------------


def test_from_mesh_creates_and_enqueues_a_manual_version(client, project, no_broker):
    response = client.post(
        FROM_MESH_URL.format(pk=project.pk),
        {"file": upload("generated.STL", box_stl())},
        format="multipart",
    )

    assert response.status_code == 201, response.content
    body = response.json()
    assert body["version"] == 1
    assert body["origin"] == "manual"
    assert body["specification_json"]["generator"] == "mesh"
    assert body["specification_json"]["mesh"]["source"] == (f"projects/{project.pk}/v1/source.stl")
    # Read-only mesh projection, not a writable file field.
    assert body["source_mesh_available"] is True
    assert body["source_mesh_url"] == f"/api/v1/versions/{body['id']}/source-mesh/"
    assert body["source_mesh_warnings"] == []
    assert body["status"] == "queued"
    assert no_broker == [body["id"]]


def test_from_mesh_forwards_transform_and_repair_fields(client, project):
    response = client.post(
        FROM_MESH_URL.format(pk=project.pk),
        {
            "file": upload("box.stl", box_stl()),
            "scale_mm": 42.5,
            "rotate_deg": [0.0, 0.0, 90.0],
            "repair": "false",
            "reference_note": "from a photo",
        },
        format="multipart",
    )

    assert response.status_code == 201, response.content
    specification = response.json()["specification_json"]
    assert specification["transform"] == {"scale_mm": 42.5, "rotate_deg": [0.0, 0.0, 90.0]}
    assert specification["repair"] == {"enabled": False}
    version = ModelVersion.objects.get(pk=response.json()["id"])
    assert version.reference_note == "from a photo"


def test_from_mesh_rejects_an_unsupported_format(client, project):
    response = client.post(
        FROM_MESH_URL.format(pk=project.pk),
        {"file": upload("model.3mf", box_stl())},
        format="multipart",
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "stl, obj, glb" in detail
    assert not ModelVersion.objects.exists()


def test_from_mesh_rejects_an_oversized_upload(client, project):
    with override_settings(MESH_MAX_SOURCE_BYTES=16):
        response = client.post(
            FROM_MESH_URL.format(pk=project.pk),
            {"file": upload("box.stl", box_stl())},
            format="multipart",
        )

    assert response.status_code == 400
    assert "above the 16 byte limit" in response.json()["detail"]
    assert not ModelVersion.objects.exists()


def test_from_mesh_rejects_a_mesh_that_is_not_printable(client, project):
    response = client.post(
        FROM_MESH_URL.format(pk=project.pk),
        {"file": upload("broken.stl", open_box_stl())},
        format="multipart",
    )

    assert response.status_code == 400
    assert "not watertight" in response.json()["detail"]
    assert not ModelVersion.objects.exists()


def test_from_mesh_requires_a_file(client, project):
    response = client.post(
        FROM_MESH_URL.format(pk=project.pk), {"scale_mm": 10}, format="multipart"
    )

    assert response.status_code == 400
    assert "file" in response.json()


def test_from_mesh_requires_a_viewer_role_to_be_a_member(client, project, viewer, no_broker):
    response = APIClient()
    response.force_authenticate(user=viewer)
    posted = response.post(
        FROM_MESH_URL.format(pk=project.pk),
        {"file": upload("box.stl", box_stl())},
        format="multipart",
    )

    assert posted.status_code == 403
    assert not ModelVersion.objects.exists()
    assert no_broker == []


def test_from_mesh_requires_authentication(project):
    response = APIClient().post(
        FROM_MESH_URL.format(pk=project.pk),
        {"file": upload("box.stl", box_stl())},
        format="multipart",
    )

    assert response.status_code in (401, 403)
    assert not ModelVersion.objects.exists()


def test_from_mesh_404s_for_a_foreign_project(client, project):
    other = User.objects.create_user(username="bob", email="bob@example.com", password="pw")
    workspace = create_workspace(name="Theirs", owner=other)
    foreign = create_project(workspace=workspace, name="Theirs", created_by=other)

    response = client.post(
        FROM_MESH_URL.format(pk=foreign.pk),
        {"file": upload("box.stl", box_stl())},
        format="multipart",
    )

    assert response.status_code == 404
    assert not ModelVersion.objects.exists()


def test_from_mesh_rejects_an_empty_file(client, project, media):
    """An empty upload is a 400 with the standard ``detail`` body, and writes nothing.

    A zero-byte mesh is the *same* kind of refusal as a bad suffix or a mesh that
    is not printable, so it answers the same shape: ``{"detail": "<English>"}`` from
    the view's ``except ValueError``, not a per-field DRF error. The empty check
    therefore lives in ``designs.services`` (see
    ``api.serializers.SourceMeshFileField``), which every other mesh refusal also
    goes through -- and which the MCP tools call directly, so both surfaces report
    one thing.
    """
    response = client.post(
        FROM_MESH_URL.format(pk=project.pk),
        {"file": upload("empty.stl", b"")},
        format="multipart",
    )

    assert response.status_code == 400
    body = response.json()
    assert list(body) == ["detail"], body
    assert "empty" in body["detail"]
    # The message also says what *is* accepted, generated from the registry.
    assert ", ".join(MESH_SOURCE_FORMATS) in body["detail"]
    assert not ModelVersion.objects.exists()
    assert not media.exists(f"projects/{project.pk}/v1/source.stl")


def test_from_mesh_rejects_a_filename_without_an_accepted_suffix(client, project):
    """No suffix means no parser to pick, so it is refused like a wrong one.

    A name with no extension at all, a dotfile and a double-suffixed name all land
    in the same readable ``400`` -- the accepted list is generated from the
    registry, never written out again here.
    """
    for filename in ("mesh", ".stl", "mesh.tar.gz", "Mesh.", "dir/mesh"):
        response = client.post(
            FROM_MESH_URL.format(pk=project.pk),
            {"file": upload(filename, box_stl())},
            format="multipart",
        )

        assert response.status_code == 400, filename
        detail = response.json()["detail"]
        assert "Unsupported source mesh" in detail, filename
        assert ", ".join(MESH_SOURCE_FORMATS) in detail, filename
    assert not ModelVersion.objects.exists()


def test_from_mesh_never_stores_the_uploaded_name(client, project, media):
    """Only the suffix of the uploaded name is used; the path is the server's.

    A client (or a browser that sends a full path) may put directories in the
    filename. They must not change where the bytes land: the stored name is always
    the per-version ``source.<ext>``, and a traversal-looking name is judged on
    its suffix like any other.
    """
    response = client.post(
        FROM_MESH_URL.format(pk=project.pk),
        {"file": upload("../../escaped.stl", box_stl())},
        format="multipart",
    )

    assert response.status_code == 201, response.content
    assert response.json()["source_mesh_format"] == "stl"
    assert media.exists(f"projects/{project.pk}/v1/source.stl")
    assert [p.name for p in media.root.rglob("escaped.stl")] == []


def test_from_mesh_rejects_unparseable_bytes(client, project):
    response = client.post(
        FROM_MESH_URL.format(pk=project.pk),
        {"file": upload("junk.stl", b"this is not a mesh at all")},
        format="multipart",
    )

    assert response.status_code == 400
    assert not ModelVersion.objects.exists()


def test_from_mesh_rejects_a_mislabelled_container(client, project):
    """The suffix picks the parser, so GLB bytes named ``.stl`` are a 400."""
    import trimesh

    payload = bytes(trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="glb"))
    response = client.post(
        FROM_MESH_URL.format(pk=project.pk),
        {"file": upload("liar.stl", payload)},
        format="multipart",
    )

    assert response.status_code == 400
    assert not ModelVersion.objects.exists()


def test_from_mesh_accepts_every_supported_container(client, project, media):
    """``stl``/``obj``/``glb`` all round-trip, and the stored name says which."""
    import trimesh

    box = trimesh.creation.box(extents=(10.0, 20.0, 30.0))
    obj = box.export(file_type="obj")
    payloads = {
        "mesh.stl": bytes(box.export(file_type="stl")),
        "mesh.obj": obj.encode("utf-8") if isinstance(obj, str) else bytes(obj),
        "mesh.glb": bytes(box.export(file_type="glb")),
    }

    for expected_version, (filename, payload) in enumerate(payloads.items(), start=1):
        suffix = filename.rsplit(".", 1)[-1]
        response = client.post(
            FROM_MESH_URL.format(pk=project.pk),
            {"file": upload(filename, payload)},
            format="multipart",
        )

        assert response.status_code == 201, response.content
        body = response.json()
        assert body["version"] == expected_version
        assert body["source_mesh_format"] == suffix
        assert media.exists(f"projects/{project.pk}/v{expected_version}/source.{suffix}")


def test_a_rejected_upload_leaves_nothing_behind(client, project, media):
    """Storage and the version table are both untouched by a rejected mesh.

    The gate runs inside the service transaction, so a 400 must not leave an
    orphan file in storage or a half-written ``ModelVersion`` row -- otherwise a
    user who retries a bad upload accumulates junk. Each case asserts the piece of
    the response that explains the refusal.
    """
    rejected = (
        ("model.3mf", box_stl(), "detail", "stl, obj, glb"),
        ("empty.stl", b"", "detail", "empty"),
        ("junk.stl", b"not a mesh", "detail", "no geometry"),
        ("broken.stl", open_box_stl(), "detail", "not watertight"),
    )

    for filename, payload, key, expected in rejected:
        response = client.post(
            FROM_MESH_URL.format(pk=project.pk),
            {"file": upload(filename, payload)},
            format="multipart",
        )
        assert response.status_code == 400, filename
        body = response.json()
        assert key in body, f"{filename}: expected {key!r} in {body}"
        assert expected in str(body[key]), filename

    assert not ModelVersion.objects.exists()
    # Nothing at all was written under the project directory.
    assert not (media.root / "projects" / str(project.pk)).exists()


def test_a_rejected_replacement_leaves_the_previous_mesh_intact(client, project, media):
    """The attach route must not disturb an existing version when the mesh is bad."""
    version = create_next_version_from_mesh(
        project=project, mesh_bytes=box_stl(), filename="good.stl", created_by=project.created_by
    )
    before = version.source_mesh.name

    response = client.post(
        f"/api/v1/versions/{version.pk}/source-mesh/",
        {"file": upload("broken.stl", open_box_stl())},
        format="multipart",
    )

    assert response.status_code == 400
    assert "not watertight" in response.json()["detail"]
    version.refresh_from_db()
    assert version.source_mesh.name == before
    assert media.exists(before)
    assert media.read_bytes(before) == box_stl()


def test_from_mesh_returns_503_when_the_broker_is_down(client, project, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("redis is down")

    monkeypatch.setattr(render_model_stl, "delay", boom)

    response = client.post(
        FROM_MESH_URL.format(pk=project.pk),
        {"file": upload("box.stl", box_stl())},
        format="multipart",
    )

    assert response.status_code == 503
    assert "queue" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# GET/POST /api/v1/versions/{id}/source-mesh/
# ---------------------------------------------------------------------------


def test_source_mesh_get_streams_the_stored_mesh(client, project):
    payload = box_stl()
    version = create_next_version_from_mesh(
        project=project, mesh_bytes=payload, filename="box.stl", created_by=project.created_by
    )

    response = client.get(f"/api/v1/versions/{version.pk}/source-mesh/")

    assert response.status_code == 200
    assert response.content == payload
    assert "source.stl" in response["Content-Disposition"]


def test_source_mesh_get_404s_without_a_mesh(client, project, user):
    version = create_next_version(project=project, prompt="parametric", created_by=user)

    response = client.get(f"/api/v1/versions/{version.pk}/source-mesh/")

    assert response.status_code == 404


def test_source_mesh_get_404s_when_the_file_is_gone(client, project, media):
    version = create_next_version_from_mesh(
        project=project, mesh_bytes=box_stl(), filename="box.stl"
    )
    media.delete(version.source_mesh.name)

    assert client.get(f"/api/v1/versions/{version.pk}/source-mesh/").status_code == 404


def test_source_mesh_post_attaches_and_re_renders(client, project, no_broker):
    version = create_next_version(project=project, prompt="v1", created_by=project.created_by)

    response = client.post(
        f"/api/v1/versions/{version.pk}/source-mesh/",
        {"file": upload("box.stl", box_stl()), "scale_mm": 60.0},
        format="multipart",
    )

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["id"] == version.pk
    assert body["specification_json"] == {
        "generator": "mesh",
        "mesh": {"source": f"projects/{project.pk}/v1/source.stl"},
        "transform": {"scale_mm": 60.0},
    }
    assert body["origin"] == "manual"
    assert body["status"] == "queued"
    assert no_broker == [version.pk]


def test_source_mesh_post_replaces_the_previous_mesh(client, project, media):
    version = create_next_version_from_mesh(
        project=project, mesh_bytes=box_stl(), filename="first.stl"
    )

    response = client.post(
        f"/api/v1/versions/{version.pk}/source-mesh/",
        {"file": upload("second.stl", box_stl()), "reference_note": "rescan"},
        format="multipart",
    )

    assert response.status_code == 200, response.content
    assert response.json()["reference_note"] == "rescan"
    assert media.exists(version.source_mesh.name)
    assert ModelVersion.objects.filter(pk=version.pk).count() == 1


def test_source_mesh_post_rejects_an_empty_file_with_the_same_body(
    client, project, media, no_broker
):
    """The replacement route answers an empty upload exactly like the import route.

    Both POST actions share the serializer and the ``except ValueError`` handler, so
    a zero-byte upload cannot be one shape on one route and another on the other --
    and the previous mesh survives, as it does for any other bad upload.
    """
    version = create_next_version_from_mesh(
        project=project, mesh_bytes=box_stl(), filename="box.stl", created_by=project.created_by
    )
    before = version.source_mesh.name

    response = client.post(
        f"/api/v1/versions/{version.pk}/source-mesh/",
        {"file": upload("empty.stl", b"")},
        format="multipart",
    )

    assert response.status_code == 400
    body = response.json()
    assert list(body) == ["detail"], body
    assert "empty" in body["detail"]
    assert no_broker == []
    version.refresh_from_db()
    assert version.source_mesh.name == before
    assert media.read_bytes(before) == box_stl()


def test_source_mesh_post_rejects_a_mesh_that_is_not_printable(client, project, no_broker):
    version = create_next_version_from_mesh(
        project=project, mesh_bytes=box_stl(), filename="box.stl"
    )

    response = client.post(
        f"/api/v1/versions/{version.pk}/source-mesh/",
        {"file": upload("broken.stl", open_box_stl())},
        format="multipart",
    )

    assert response.status_code == 400
    assert "not watertight" in response.json()["detail"]
    assert no_broker == []
    version.refresh_from_db()
    assert version.source_mesh.name.endswith("source.stl")


def test_source_mesh_post_requires_a_viewer_role_to_be_a_member(project, viewer, no_broker):
    version = create_next_version_from_mesh(
        project=project, mesh_bytes=box_stl(), filename="box.stl"
    )
    client = APIClient()
    client.force_authenticate(user=viewer)

    response = client.post(
        f"/api/v1/versions/{version.pk}/source-mesh/",
        {"file": upload("other.stl", box_stl())},
        format="multipart",
    )

    assert response.status_code == 403
    assert no_broker == []


def test_source_mesh_get_is_readable_by_a_viewer(project, viewer):
    version = create_next_version_from_mesh(
        project=project, mesh_bytes=box_stl(), filename="box.stl"
    )
    client = APIClient()
    client.force_authenticate(user=viewer)

    response = client.get(f"/api/v1/versions/{version.pk}/source-mesh/")

    assert response.status_code == 200


def test_source_mesh_post_requires_authentication(project):
    version = create_next_version_from_mesh(
        project=project, mesh_bytes=box_stl(), filename="box.stl"
    )

    response = APIClient().post(
        f"/api/v1/versions/{version.pk}/source-mesh/",
        {"file": upload("other.stl", box_stl())},
        format="multipart",
    )

    assert response.status_code in (401, 403)
    assert ModelVersion.objects.filter(pk=version.pk).exists()


def test_source_mesh_404s_for_a_foreign_version(client, project):
    other = User.objects.create_user(username="bob", email="bob@example.com", password="pw")
    workspace = create_workspace(name="Theirs", owner=other)
    foreign = create_project(workspace=workspace, name="Theirs", created_by=other)
    version = create_next_version_from_mesh(
        project=foreign, mesh_bytes=box_stl(), filename="box.stl"
    )

    assert client.get(f"/api/v1/versions/{version.pk}/source-mesh/").status_code == 404
    assert (
        client.post(
            f"/api/v1/versions/{version.pk}/source-mesh/",
            {"file": upload("other.stl", box_stl())},
            format="multipart",
        ).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# The ``source_mesh_format`` serializer field
# ---------------------------------------------------------------------------
#
# The download route carries no file extension, so this read-only projection is
# the only way a client learns which parser to use. It has to be right per stored
# format, empty when there is nothing to download, and it must not be writable --
# a client cannot smuggle in a different container by posting a field.


def _detail(client, version) -> dict:
    response = client.get(f"/api/v1/versions/{version.pk}/")
    assert response.status_code == 200, response.content
    return response.json()


def test_source_mesh_format_reports_the_stored_container(client, project):
    import trimesh

    box = trimesh.creation.box(extents=(10.0, 20.0, 30.0))
    obj = box.export(file_type="obj")
    payloads = {
        "mesh.stl": bytes(box.export(file_type="stl")),
        "mesh.obj": obj.encode("utf-8") if isinstance(obj, str) else bytes(obj),
        "mesh.glb": bytes(box.export(file_type="glb")),
    }

    for filename, payload in payloads.items():
        version = create_next_version_from_mesh(
            project=project, mesh_bytes=payload, filename=filename
        )

        body = _detail(client, version)
        assert body["source_mesh_format"] == filename.rsplit(".", 1)[-1]
        assert body["source_mesh_available"] is True
        assert body["source_mesh_url"] == f"/api/v1/versions/{version.pk}/source-mesh/"


def test_source_mesh_format_is_empty_for_a_parametric_version(client, project):
    version = create_next_version(project=project, prompt="a parametric box")

    body = _detail(client, version)

    assert body["source_mesh_format"] == ""
    assert body["source_mesh_available"] is False
    assert body["source_mesh_url"] is None
    assert body["source_mesh_warnings"] == []


def test_source_mesh_format_is_empty_for_an_unknown_suffix(client, project):
    """Never raises: a hand-written name reports ``""`` instead of erroring."""
    version = create_next_version(project=project, prompt="parametric")
    version.source_mesh.name = f"projects/{project.pk}/v1/source.step"
    version.save(update_fields=["source_mesh"])

    assert _detail(client, version)["source_mesh_format"] == ""


def test_source_mesh_format_is_read_only(client, project, media):
    """Posting the field must not change what the version actually stores."""
    version = create_next_version_from_mesh(
        project=project, mesh_bytes=box_stl(), filename="box.stl", created_by=project.created_by
    )

    response = client.post(
        f"/api/v1/versions/{version.pk}/source-mesh/",
        {
            "file": upload("box.stl", box_stl()),
            # A client trying to override the projection, or the raw file field.
            "source_mesh_format": "glb",
            "source_mesh": f"projects/{project.pk}/v1/evil.glb",
        },
        format="multipart",
    )

    assert response.status_code == 200, response.content
    version.refresh_from_db()
    assert version.source_mesh.name == f"projects/{project.pk}/v1/source.stl"
    assert _detail(client, version)["source_mesh_format"] == "stl"
    # The forged file field never reached storage.
    assert [p.name for p in media.root.rglob("evil.glb")] == []
