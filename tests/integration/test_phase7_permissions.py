"""Phase 7 integration tests (terv.md 20., 21. Phase 7).

Covers the multi-user contract end to end through the HTTP API:

* the workspace role matrix (VIEWER read, MEMBER write projects/versions,
  ADMIN/OWNER manage the workspace, non-members get 404/403 with no leak);
* workspace scoping (tenant A never sees tenant B's resources);
* the ``PRINTER_OPERATOR`` gate on print-job control;
* notifications (own-only, idempotent read, read-all counts);
* the login-protected ``printers:history`` page.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from factories import (
    ModelVersionFactory,
    NotificationFactory,
    PrinterFactory,
    PrinterProfileFactory,
    PrintJobFactory,
    ProjectFactory,
    UserFactory,
    WorkspaceFactory,
)
from rest_framework.test import APIClient

from agents.services import start_run
from designs.tasks import render_model_stl
from files.services import LocalStorage
from notifications.services import mark_all_read, mark_read, notify, unread_count
from printers.models import PrintJob, PrintJobStatus
from printers.services import job_status, jobs_for_user, set_slicing_metadata
from workspaces.models import WorkspaceRole
from workspaces.services import add_member

pytestmark = pytest.mark.django_db


def auth(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def printer():
    return PrinterFactory()


@pytest.fixture
def operator():
    """A staff user -- the current stand-in for PRINTER_OPERATOR."""
    return UserFactory(is_staff=True)


# ---------------------------------------------------------------------------
# Role matrix
# ---------------------------------------------------------------------------


def test_viewer_can_read_but_cannot_write(workspace, project, version):
    viewer = UserFactory()
    add_member(workspace=workspace, user=viewer, role=WorkspaceRole.VIEWER)
    client = auth(viewer)

    assert client.get("/api/v1/workspaces/").status_code == 200
    assert client.get(f"/api/v1/projects/{project.id}/").status_code == 200
    assert client.get(f"/api/v1/projects/{project.id}/versions/").status_code == 200
    assert client.get(f"/api/v1/versions/{version.id}/").status_code == 200

    assert (
        client.post(
            "/api/v1/projects/", {"workspace": workspace.id, "name": "X"}, format="json"
        ).status_code
        == 403
    )
    assert (
        client.patch(f"/api/v1/projects/{project.id}/", {"name": "X"}, format="json").status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/projects/{project.id}/versions/", {"prompt": "x"}, format="json"
        ).status_code
        == 403
    )
    assert (
        client.patch(
            f"/api/v1/workspaces/{workspace.id}/", {"name": "X"}, format="json"
        ).status_code
        == 403
    )


def test_member_can_write_projects_but_not_manage_the_workspace(workspace, monkeypatch):
    monkeypatch.setattr(render_model_stl, "delay", lambda pk: None)
    member = UserFactory()
    add_member(workspace=workspace, user=member, role=WorkspaceRole.MEMBER)
    client = auth(member)

    created = client.post(
        "/api/v1/projects/", {"workspace": workspace.id, "name": "Member project"}, format="json"
    )
    assert created.status_code == 201, created.content
    assert created.json()["created_by"] == member.id

    version = client.post(
        f"/api/v1/projects/{created.json()['id']}/versions/",
        {"prompt": "make it"},
        format="json",
    )
    assert version.status_code == 201, version.content

    # Workspace management is ADMIN+.
    assert (
        client.patch(
            f"/api/v1/workspaces/{workspace.id}/", {"name": "M"}, format="json"
        ).status_code
        == 403
    )
    assert client.delete(f"/api/v1/workspaces/{workspace.id}/").status_code == 403


def test_admin_and_owner_can_manage_the_workspace(workspace, user):
    admin = UserFactory()
    add_member(workspace=workspace, user=admin, role=WorkspaceRole.ADMIN)

    as_admin = auth(admin).patch(
        f"/api/v1/workspaces/{workspace.id}/", {"name": "Admin renamed"}, format="json"
    )
    assert as_admin.status_code == 200, as_admin.content

    as_owner = auth(user).patch(
        f"/api/v1/workspaces/{workspace.id}/", {"name": "Owner renamed"}, format="json"
    )
    assert as_owner.status_code == 200, as_owner.content

    workspace.refresh_from_db()
    assert workspace.name == "Owner renamed"


def test_non_member_gets_404_without_existence_leak(workspace, project, version, other_user):
    client = auth(other_user)

    assert client.get("/api/v1/workspaces/").json()["results"] == []
    assert client.get("/api/v1/projects/").json()["results"] == []

    assert client.get(f"/api/v1/workspaces/{workspace.id}/").status_code == 404
    assert client.get(f"/api/v1/projects/{project.id}/").status_code == 404
    assert client.get(f"/api/v1/projects/{project.id}/versions/").status_code == 404
    assert client.get(f"/api/v1/versions/{version.id}/").status_code == 404

    # Creating into a foreign workspace is refused outright.
    assert (
        client.post(
            "/api/v1/projects/", {"workspace": workspace.id, "name": "Intruder"}, format="json"
        ).status_code
        == 403
    )


# ---------------------------------------------------------------------------
# Workspace scoping (no cross-tenant leakage)
# ---------------------------------------------------------------------------


def test_workspace_scoping_isolates_tenants(printer):
    alice = UserFactory()
    bob = UserFactory()
    ws_a = WorkspaceFactory(owner=alice)
    ws_b = WorkspaceFactory(owner=bob)
    project_a = ProjectFactory(workspace=ws_a, created_by=alice)
    project_b = ProjectFactory(workspace=ws_b, created_by=bob)
    version_a = ModelVersionFactory(project=project_a, created_by=alice)
    version_b = ModelVersionFactory(project=project_b, created_by=bob)
    job_a = PrintJobFactory(project=project_a, printer=printer)
    job_b = PrintJobFactory(project=project_b, printer=printer)

    client = auth(alice)

    assert [w["id"] for w in client.get("/api/v1/workspaces/").json()["results"]] == [ws_a.id]
    assert [p["id"] for p in client.get("/api/v1/projects/").json()["results"]] == [project_a.id]
    assert [j["id"] for j in client.get("/api/v1/print-jobs/").json()["results"]] == [job_a.id]

    assert client.get(f"/api/v1/workspaces/{ws_b.id}/").status_code == 404
    assert client.get(f"/api/v1/projects/{project_b.id}/").status_code == 404
    assert client.get(f"/api/v1/projects/{project_b.id}/versions/").status_code == 404
    assert client.get(f"/api/v1/versions/{version_b.id}/").status_code == 404
    assert client.get(f"/api/v1/print-jobs/{job_b.id}/").status_code == 404

    # Alice still sees her own version.
    assert client.get(f"/api/v1/versions/{version_a.id}/").status_code == 200


def test_jobs_for_user_does_not_leak_across_workspaces(printer):
    alice = UserFactory()
    bob = UserFactory()
    ws_a = WorkspaceFactory(owner=alice)
    ws_b = WorkspaceFactory(owner=bob)
    job_a = PrintJobFactory(
        project=ProjectFactory(workspace=ws_a, created_by=alice), printer=printer
    )
    job_b = PrintJobFactory(project=ProjectFactory(workspace=ws_b, created_by=bob), printer=printer)

    assert set(jobs_for_user(alice).values_list("id", flat=True)) == {job_a.id}
    assert set(jobs_for_user(bob).values_list("id", flat=True)) == {job_b.id}


def test_print_job_list_uses_scoped_jobs_for_user(workspace, project, printer, user):
    job = PrintJobFactory(project=project, printer=printer)

    with patch("api.views.jobs_for_user", wraps=jobs_for_user) as spy:
        response = auth(user).get("/api/v1/print-jobs/")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["results"]] == [job.id]
    spy.assert_called()


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def test_notifications_are_own_only():
    owner = UserFactory()
    other = UserFactory()
    mine = NotificationFactory(user=owner)
    theirs = NotificationFactory(user=other)
    client = auth(owner)

    results = client.get("/api/v1/notifications/").json()["results"]
    assert [item["id"] for item in results] == [mine.id]
    assert client.get(f"/api/v1/notifications/{theirs.id}/").status_code == 404


def test_mark_read_is_idempotent_and_foreign_is_404():
    owner = UserFactory()
    other = UserFactory()
    mine = NotificationFactory(user=owner)
    theirs = NotificationFactory(user=other)
    client = auth(owner)

    first = client.post(f"/api/v1/notifications/{mine.id}/read/")
    assert first.status_code == 200
    assert first.json()["is_read"] is True

    second = client.post(f"/api/v1/notifications/{mine.id}/read/")
    assert second.status_code == 200
    assert second.json()["is_read"] is True

    mine.refresh_from_db()
    assert mine.is_read is True

    assert client.post(f"/api/v1/notifications/{theirs.id}/read/").status_code == 404
    theirs.refresh_from_db()
    assert theirs.is_read is False


def test_read_all_returns_counts():
    owner = UserFactory()
    NotificationFactory.create_batch(3, user=owner)
    NotificationFactory(user=owner, is_read=True)
    client = auth(owner)

    response = client.post("/api/v1/notifications/read-all/")

    assert response.status_code == 200
    assert response.json() == {"updated": 3, "unread": 0}
    assert unread_count(owner) == 0


def test_notification_service_contract():
    owner = UserFactory()
    notification = notify(user=owner, message="hello", kind="print", url="/x")

    assert notification.kind == "print"
    assert unread_count(owner) == 1
    assert mark_read(owner, notification.pk).is_read is True
    assert mark_read(owner, 10_000_000) is None

    notify(user=owner, message="second")
    assert mark_all_read(owner) == 1
    assert unread_count(owner) == 0


# ---------------------------------------------------------------------------
# Print jobs: membership + printer-operator gate
# ---------------------------------------------------------------------------


def test_enqueue_requires_membership(project, version, printer, other_user):
    response = auth(other_user).post(
        "/api/v1/print-jobs/",
        {"project": project.id, "model_version": version.id, "printer": printer.id},
        format="json",
    )

    assert response.status_code == 403
    assert PrintJob.objects.count() == 0


def test_viewer_cannot_enqueue(workspace, project, version, printer):
    viewer = UserFactory()
    add_member(workspace=workspace, user=viewer, role=WorkspaceRole.VIEWER)

    response = auth(viewer).post(
        "/api/v1/print-jobs/",
        {"project": project.id, "model_version": version.id, "printer": printer.id},
        format="json",
    )

    assert response.status_code == 403
    assert PrintJob.objects.count() == 0


def test_member_can_enqueue_and_printer_profile_is_optional(workspace, project, version, printer):
    member = UserFactory()
    add_member(workspace=workspace, user=member, role=WorkspaceRole.MEMBER)
    client = auth(member)

    response = client.post(
        "/api/v1/print-jobs/",
        {"project": project.id, "model_version": version.id, "printer": printer.id},
        format="json",
    )
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["printer_profile"] is None
    assert body["status"] == PrintJobStatus.QUEUED
    assert PrintJob.objects.get(pk=body["id"]).created_by_id == member.id

    profile = PrinterProfileFactory()
    with_profile = client.post(
        "/api/v1/print-jobs/",
        {
            "project": project.id,
            "model_version": version.id,
            "printer": printer.id,
            "printer_profile": profile.id,
        },
        format="json",
    )
    assert with_profile.status_code == 201, with_profile.content
    assert with_profile.json()["printer_profile"] == profile.id


def test_transition_start_cancel_require_operator(workspace, project, printer):
    member = UserFactory()
    add_member(workspace=workspace, user=member, role=WorkspaceRole.MEMBER)
    job = PrintJobFactory(project=project, printer=printer, created_by=member)
    client = auth(member)

    assert (
        client.post(
            f"/api/v1/print-jobs/{job.id}/transition/",
            {"status": PrintJobStatus.PREPARING},
            format="json",
        ).status_code
        == 403
    )
    assert client.post(f"/api/v1/print-jobs/{job.id}/start/").status_code == 403
    assert client.post(f"/api/v1/print-jobs/{job.id}/cancel/").status_code == 403

    job.refresh_from_db()
    assert job.status == PrintJobStatus.QUEUED


def test_operator_can_transition_and_cancel(workspace, project, printer, operator):
    add_member(workspace=workspace, user=operator, role=WorkspaceRole.MEMBER)
    job = PrintJobFactory(project=project, printer=printer)
    client = auth(operator)

    moved = client.post(
        f"/api/v1/print-jobs/{job.id}/transition/",
        {"status": PrintJobStatus.PREPARING},
        format="json",
    )
    assert moved.status_code == 200, moved.content
    assert moved.json()["status"] == PrintJobStatus.PREPARING

    ready = client.post(
        f"/api/v1/print-jobs/{job.id}/transition/",
        {"status": PrintJobStatus.READY},
        format="json",
    )
    assert ready.status_code == 200

    cancelled = client.post(f"/api/v1/print-jobs/{job.id}/cancel/")
    assert cancelled.status_code == 200, cancelled.content
    assert cancelled.json()["status"] == PrintJobStatus.CANCELLED


class _FakePrinterBackend:
    """Duck-typed adapter recording the upload/start calls (no network)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def upload(self, data: bytes, filename: str) -> str:
        self.calls.append(("upload", data, filename))
        return "remote.gcode"

    def start(self, filename: str) -> None:
        self.calls.append(("start", filename))

    def cancel(self) -> None:
        self.calls.append(("cancel",))


def test_operator_can_start_a_ready_job(
    workspace, project, printer, operator, settings, tmp_path, monkeypatch
):
    add_member(workspace=workspace, user=operator, role=WorkspaceRole.MEMBER)
    settings.MEDIA_ROOT = str(tmp_path)
    storage = LocalStorage(root=tmp_path)
    job = PrintJobFactory(project=project, printer=printer, status=PrintJobStatus.READY)
    relative = f"print_jobs/{project.id}/job.gcode"
    storage.write_bytes(relative, b"G28\n")
    job.gcode.name = relative
    job.save(update_fields=["gcode"])

    backend = _FakePrinterBackend()
    monkeypatch.setattr("printers.services.get_printer_backend", lambda p: backend)
    monkeypatch.setattr("printers.services.get_storage", lambda: storage)

    response = auth(operator).post(f"/api/v1/print-jobs/{job.id}/start/")

    assert response.status_code == 200, response.content
    assert response.json()["status"] == PrintJobStatus.PRINTING
    assert backend.calls[0][0] == "upload"
    job.refresh_from_db()
    assert job.status == PrintJobStatus.PRINTING


def test_slicing_json_persists_estimates(project, printer):
    job = PrintJobFactory(project=project, printer=printer)
    assert job.slicing_json == {}

    set_slicing_metadata(job, format="gcode", gcode_bytes=10)
    set_slicing_metadata(
        job, estimate={"minutes": 3, "grams": 1.2}, computed_at="2026-01-01T00:00:00Z"
    )

    job.refresh_from_db()
    assert job.slicing_json["format"] == "gcode"
    assert job.slicing_json["gcode_bytes"] == 10
    assert job.slicing_json["estimate"] == {"minutes": 3, "grams": 1.2}
    assert job.slicing_json["computed_at"] == "2026-01-01T00:00:00Z"
    assert job_status(job)["slicing"]["estimate"] == {"minutes": 3, "grams": 1.2}


def test_foreign_print_job_control_actions_are_404(project, printer):
    # A staff operator would pass IsPrinterOperator, so only workspace scoping
    # can stop this: the response must be 404 (not 403/200).
    outsider = UserFactory(is_staff=True)
    job = PrintJobFactory(project=project, printer=printer)
    client = auth(outsider)

    assert (
        client.post(
            f"/api/v1/print-jobs/{job.id}/transition/",
            {"status": PrintJobStatus.PREPARING},
            format="json",
        ).status_code
        == 404
    )
    assert client.post(f"/api/v1/print-jobs/{job.id}/start/").status_code == 404
    assert client.post(f"/api/v1/print-jobs/{job.id}/cancel/").status_code == 404

    job.refresh_from_db()
    assert job.status == PrintJobStatus.QUEUED


def test_enqueue_rejects_version_from_another_project(workspace, project, printer):
    member = UserFactory()
    add_member(workspace=workspace, user=member, role=WorkspaceRole.MEMBER)
    other_project = ProjectFactory(workspace=workspace, created_by=workspace.owner)
    other_version = ModelVersionFactory(project=other_project)

    response = auth(member).post(
        "/api/v1/print-jobs/",
        {
            "project": project.id,
            "model_version": other_version.id,
            "printer": printer.id,
        },
        format="json",
    )

    assert response.status_code == 400
    assert PrintJob.objects.count() == 0


def test_non_member_cannot_update_a_foreign_project(project, other_user):
    response = auth(other_user).patch(
        f"/api/v1/projects/{project.id}/", {"name": "Hijacked"}, format="json"
    )

    assert response.status_code == 404
    project.refresh_from_db()
    assert project.name != "Hijacked"


def test_agent_runs_are_workspace_scoped():
    alice = UserFactory()
    bob = UserFactory()
    project_a = ProjectFactory(workspace=WorkspaceFactory(owner=alice), created_by=alice)
    project_b = ProjectFactory(workspace=WorkspaceFactory(owner=bob), created_by=bob)
    run_a = start_run(project=project_a, user_prompt="a")
    run_b = start_run(project=project_b, user_prompt="b")

    client = auth(alice)

    results = client.get("/api/v1/agent-runs/").json()["results"]
    assert [item["id"] for item in results] == [run_a.id]
    assert client.get(f"/api/v1/agent-runs/{run_b.id}/").status_code == 404


def test_enqueue_cannot_target_a_foreign_workspace_project():
    """Regression for the cross-workspace enqueue bypass (fixed in Phase 7).

    The permission layer now trusts only the endpoint's writable serializer
    fields, so the ignored ``workspace`` key can no longer authorize a foreign
    ``project``.
    """
    attacker = UserFactory()
    attacker_workspace = WorkspaceFactory(owner=attacker)
    victim_workspace = WorkspaceFactory()
    victim_project = ProjectFactory(workspace=victim_workspace)
    victim_version = ModelVersionFactory(project=victim_project)
    printer = PrinterFactory()

    response = auth(attacker).post(
        "/api/v1/print-jobs/",
        {
            "workspace": attacker_workspace.id,
            "project": victim_project.id,
            "model_version": victim_version.id,
            "printer": printer.id,
        },
        format="json",
    )

    assert response.status_code in (403, 404)


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


def test_print_history_requires_login(client):
    response = client.get("/printers/history/")

    assert response.status_code == 302
    assert "/login/" in response["Location"]


def test_print_history_renders_for_authenticated_user(client, user):
    client.force_login(user)

    response = client.get("/printers/history/")

    assert response.status_code == 200


def test_printer_list_page_requires_login(client):
    response = client.get("/printers/")

    assert response.status_code == 302
    assert "/login/" in response["Location"]


def test_printer_list_page_renders_for_authenticated_user(client, user):
    client.force_login(user)

    response = client.get("/printers/")

    assert response.status_code == 200
