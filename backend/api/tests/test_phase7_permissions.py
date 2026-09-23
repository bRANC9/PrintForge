"""Phase 7 tests: object-level workspace permissions and the new endpoints."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from agents.tasks import run_agent_workflow
from designs.services import create_next_version
from designs.tasks import render_model_stl
from notifications.services import mark_all_read, mark_read, notify, unread_count
from printers.models import Printer, PrintJobStatus
from printers.services import cancel_job as real_cancel_job
from printers.services import enqueue_job
from printers.services import transition as real_transition
from projects.services import create_project
from slicers.models import PrinterProfile
from workspaces.models import WorkspaceRole
from workspaces.services import add_member, create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()


def make_user(username, **extra):
    return User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password="pw",
        **extra,
    )


def auth(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def owner():
    return make_user("owner")


@pytest.fixture
def outsider():
    return make_user("outsider")


@pytest.fixture
def workspace(owner):
    return create_workspace(name="Lab", owner=owner)


@pytest.fixture
def project(workspace, owner):
    return create_project(workspace=workspace, name="Holder", created_by=owner)


@pytest.fixture
def version(project, owner):
    return create_next_version(project=project, prompt="make it", created_by=owner)


# ---------------------------------------------------------------------------
# Authentication is required everywhere except health
# ---------------------------------------------------------------------------


def test_health_is_public():
    assert APIClient().get("/api/v1/health/").status_code == 200


@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/workspaces/",
        "/api/v1/projects/",
        "/api/v1/versions/1/",
        "/api/v1/agent-runs/",
        "/api/v1/print-jobs/",
        "/api/v1/notifications/",
    ],
)
def test_anonymous_reads_are_rejected(url):
    assert APIClient().get(url).status_code in (401, 403)


# ---------------------------------------------------------------------------
# Role matrix
# ---------------------------------------------------------------------------


def test_non_member_sees_nothing_and_gets_404_details(outsider, workspace, project, version):
    client = auth(outsider)

    assert client.get("/api/v1/workspaces/").json()["results"] == []
    assert client.get(f"/api/v1/projects/{project.pk}/").status_code == 404
    assert client.get(f"/api/v1/versions/{version.pk}/").status_code == 404
    assert client.get(f"/api/v1/projects/{project.pk}/versions/").status_code == 404


def test_non_member_cannot_create_into_a_foreign_workspace(outsider, workspace):
    response = auth(outsider).post(
        "/api/v1/projects/",
        {"workspace": workspace.pk, "name": "Intruder"},
        format="json",
    )
    assert response.status_code == 403


def test_viewer_can_read_but_not_write(workspace, project, version):
    viewer = make_user("viewer")
    add_member(workspace=workspace, user=viewer, role=WorkspaceRole.VIEWER)
    client = auth(viewer)

    assert client.get(f"/api/v1/projects/{project.pk}/").status_code == 200
    assert client.get(f"/api/v1/projects/{project.pk}/versions/").status_code == 200
    assert client.get(f"/api/v1/versions/{version.pk}/").status_code == 200

    assert (
        client.post(
            "/api/v1/projects/", {"workspace": workspace.pk, "name": "X"}, format="json"
        ).status_code
        == 403
    )
    assert (
        client.patch(f"/api/v1/projects/{project.pk}/", {"name": "X"}, format="json").status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/projects/{project.pk}/versions/", {"prompt": "x"}, format="json"
        ).status_code
        == 403
    )


def test_member_can_create_projects_and_versions(workspace, monkeypatch):
    monkeypatch.setattr(render_model_stl, "delay", lambda pk: None)
    enqueued: list[tuple] = []

    def record(*args, **kwargs):
        enqueued.append((args, kwargs))

    # A prompt-only version now goes through the AI agent workflow (202); the
    # agent task creates the version, so record its enqueue instead of the
    # synchronous render.
    monkeypatch.setattr(run_agent_workflow, "delay", record)
    member = make_user("member")
    add_member(workspace=workspace, user=member, role=WorkspaceRole.MEMBER)
    client = auth(member)

    created = client.post(
        "/api/v1/projects/", {"workspace": workspace.pk, "name": "New"}, format="json"
    )
    assert created.status_code == 201

    version = client.post(
        f"/api/v1/projects/{created.json()['id']}/versions/",
        {"prompt": "make it"},
        format="json",
    )
    assert version.status_code == 202, version.content
    assert version.json() == {"status": "queued", "mode": "agent"}

    ((args, kwargs),) = enqueued
    assert args == (created.json()["id"], "make it", member.pk)
    assert kwargs == {
        "reference_image_name": "",
        "reference_note": "",
        "skill_ids": [],
        "auto_skill_selection": False,
        "clarify_policy": "assume",
    }


def test_only_admin_can_update_a_workspace(workspace):
    member = make_user("member2")
    admin = make_user("admin")
    add_member(workspace=workspace, user=member, role=WorkspaceRole.MEMBER)
    add_member(workspace=workspace, user=admin, role=WorkspaceRole.ADMIN)

    assert (
        auth(member)
        .patch(f"/api/v1/workspaces/{workspace.pk}/", {"name": "M"}, format="json")
        .status_code
        == 403
    )
    assert (
        auth(admin)
        .patch(f"/api/v1/workspaces/{workspace.pk}/", {"name": "A"}, format="json")
        .status_code
        == 200
    )


# ---------------------------------------------------------------------------
# Print jobs
# ---------------------------------------------------------------------------


def test_print_job_enqueue_and_transition_permissions(workspace, project, version):
    printer = Printer.objects.create(name="K2 Pro")
    member = make_user("member3")
    add_member(workspace=workspace, user=member, role=WorkspaceRole.MEMBER)
    client = auth(member)

    created = client.post(
        "/api/v1/print-jobs/",
        {
            "project": project.pk,
            "model_version": version.pk,
            "printer": printer.pk,
            "priority": 5,
        },
        format="json",
    )
    assert created.status_code == 201
    body = created.json()
    assert body["printer_name"] == "K2 Pro"
    assert body["project_name"] == project.name
    assert body["version"] == version.version
    assert body["filament_name"] is None
    assert body["status"] == PrintJobStatus.QUEUED

    # A plain MEMBER is not a PRINTER_OPERATOR.
    assert (
        client.post(
            f"/api/v1/print-jobs/{body['id']}/transition/",
            {"status": "PREPARING"},
            format="json",
        ).status_code
        == 403
    )

    operator = make_user("operator", is_staff=True)
    add_member(workspace=workspace, user=operator, role=WorkspaceRole.MEMBER)
    moved = auth(operator).post(
        f"/api/v1/print-jobs/{body['id']}/transition/",
        {"status": "PREPARING"},
        format="json",
    )
    assert moved.status_code == 200
    assert moved.json()["status"] == "PREPARING"

    invalid = auth(operator).post(
        f"/api/v1/print-jobs/{body['id']}/transition/",
        {"status": "COMPLETED"},
        format="json",
    )
    assert invalid.status_code == 400


def test_print_job_accepts_a_printer_profile(workspace, project, version):
    printer = Printer.objects.create(name="K2 Pro")
    profile = PrinterProfile.objects.create(name="K2 0.4 nozzle")
    member = make_user("member5")
    add_member(workspace=workspace, user=member, role=WorkspaceRole.MEMBER)

    response = auth(member).post(
        "/api/v1/print-jobs/",
        {
            "project": project.pk,
            "model_version": version.pk,
            "printer": printer.pk,
            "printer_profile": profile.pk,
        },
        format="json",
    )

    assert response.status_code == 201
    assert response.json()["printer_profile"] == profile.pk


def test_print_jobs_are_scoped_to_members(workspace, project, version, outsider):
    printer = Printer.objects.create(name="K2 Pro")
    member = make_user("member4")
    add_member(workspace=workspace, user=member, role=WorkspaceRole.MEMBER)
    created = auth(member).post(
        "/api/v1/print-jobs/",
        {"project": project.pk, "model_version": version.pk, "printer": printer.pk},
        format="json",
    )
    assert created.status_code == 201

    assert auth(outsider).get("/api/v1/print-jobs/").json()["results"] == []
    assert auth(outsider).get(f"/api/v1/print-jobs/{created.json()['id']}/").status_code == 404


def test_start_and_cancel_require_operator(workspace, project, version):
    printer = Printer.objects.create(name="K2 Pro")
    member = make_user("member6")
    add_member(workspace=workspace, user=member, role=WorkspaceRole.MEMBER)
    job = enqueue_job(project=project, model_version=version, printer=printer, created_by=member)

    assert auth(member).post(f"/api/v1/print-jobs/{job.pk}/start/").status_code == 403
    assert auth(member).post(f"/api/v1/print-jobs/{job.pk}/cancel/").status_code == 403

    operator = make_user("operator3", is_staff=True)
    add_member(workspace=workspace, user=operator, role=WorkspaceRole.MEMBER)
    cancelled = auth(operator).post(f"/api/v1/print-jobs/{job.pk}/cancel/")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == PrintJobStatus.CANCELLED


def test_start_requires_a_ready_job(workspace, project, version):
    printer = Printer.objects.create(name="K2 Pro")
    operator = make_user("operator4", is_staff=True)
    add_member(workspace=workspace, user=operator, role=WorkspaceRole.MEMBER)
    job = enqueue_job(project=project, model_version=version, printer=printer)

    response = auth(operator).post(f"/api/v1/print-jobs/{job.pk}/start/")

    assert response.status_code == 400


def test_control_actions_forward_user_to_the_service(workspace, project, version):
    printer = Printer.objects.create(name="K2 Pro")
    operator = make_user("operator5", is_staff=True)
    add_member(workspace=workspace, user=operator, role=WorkspaceRole.MEMBER)
    job = enqueue_job(project=project, model_version=version, printer=printer)
    client = auth(operator)

    with patch("api.views.transition_print_job", wraps=real_transition) as spy:
        moved = client.post(
            f"/api/v1/print-jobs/{job.pk}/transition/",
            {"status": "PREPARING"},
            format="json",
        )
    assert moved.status_code == 200
    assert spy.call_args.kwargs["user"] == operator

    with patch("api.views.cancel_job", wraps=real_cancel_job) as spy:
        cancelled = client.post(f"/api/v1/print-jobs/{job.pk}/cancel/")
    assert cancelled.status_code == 200
    assert spy.call_args.kwargs["user"] == operator


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def test_notifications_are_own_only_and_mark_read_is_idempotent(owner, outsider):
    mine = notify(user=owner, message="hello", kind="print")
    theirs = notify(user=outsider, message="secret")
    client = auth(owner)

    results = client.get("/api/v1/notifications/").json()["results"]
    assert [item["id"] for item in results] == [mine.pk]

    assert client.post(f"/api/v1/notifications/{theirs.pk}/read/").status_code == 404

    first = client.post(f"/api/v1/notifications/{mine.pk}/read/")
    assert first.status_code == 200
    assert first.json()["is_read"] is True

    second = client.post(f"/api/v1/notifications/{mine.pk}/read/")
    assert second.status_code == 200
    assert second.json()["is_read"] is True


def test_read_all_marks_every_unread_notification(owner):
    notify(user=owner, message="a")
    notify(user=owner, message="b")
    client = auth(owner)

    response = client.post("/api/v1/notifications/read-all/")

    assert response.status_code == 200
    assert response.json()["updated"] == 2
    assert unread_count(owner) == 0


def test_notification_service_contract(owner):
    notification = notify(user=owner, message="m", kind="k", url="/x")

    assert unread_count(owner) == 1
    assert mark_read(owner, notification.pk).is_read is True
    assert mark_read(owner, 999_999) is None

    notify(user=owner, message="n")
    assert mark_all_read(owner) == 1
