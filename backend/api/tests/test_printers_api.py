"""Printer registry + live status API (terv.md 13. fejezet).

The adapter is monkeypatched, so no network/printer is needed. The key
guarantees: authentication is required, ``host``/``api_key`` never leak, and a
backend failure is reported as ``online: false`` rather than a 5xx.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from printers.base import CfsSlot, PrinterConnectionError, PrinterState, PrinterStatus
from printers.models import Printer

pytestmark = pytest.mark.django_db

User = get_user_model()


def auth(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def user():
    return User.objects.create_user(username="ada", email="ada@example.com", password="pw")


@pytest.fixture
def staff():
    return User.objects.create_user(
        username="admin", email="admin@example.com", password="pw", is_staff=True
    )


@pytest.fixture
def printer():
    return Printer.objects.create(
        name="K2 Pro", backend="creality_k2", host="10.0.0.7", api_key="super-secret"
    )


def test_printer_list_requires_authentication(printer):
    assert APIClient().get("/api/v1/printers/").status_code in (401, 403)


def test_printer_list_hides_host_and_api_key(printer, user):
    response = auth(user).get("/api/v1/printers/")

    assert response.status_code == 200
    row = response.json()["results"][0]
    assert row == {
        "id": printer.id,
        "name": "K2 Pro",
        "backend": "creality_k2",
        "is_active": True,
        "created_at": row["created_at"],
    }
    assert "host" not in row
    assert "api_key" not in row


def test_printer_status_returns_the_snapshot(printer, user, monkeypatch):
    monkeypatch.setattr(
        "api.views.printer_status",
        lambda _printer: PrinterStatus(
            online=True,
            state=PrinterState.PRINTING,
            current_filename="cube.gcode",
            progress=0.5,
            message="printing",
        ),
    )
    monkeypatch.setattr(
        "api.views.printer_cfs_slots",
        lambda _printer: [
            CfsSlot(index=0, material="PLA", brand="Creality", name="Hyper PLA", empty=False)
        ],
    )

    response = auth(user).get(f"/api/v1/printers/{printer.pk}/status/")

    assert response.status_code == 200
    body = response.json()
    assert body["online"] is True
    assert body["state"] == "printing"
    assert body["current_filename"] == "cube.gcode"
    assert body["progress"] == 0.5
    assert body["cfs_slots"] == [
        {
            "index": 0,
            "material": "PLA",
            "brand": "Creality",
            "name": "Hyper PLA",
            "color": "",
            "state": "",
            "empty": False,
        }
    ]


def test_printer_status_reports_a_backend_failure_as_offline(printer, user, monkeypatch):
    def boom(_printer):
        raise PrinterConnectionError("cannot reach Moonraker")

    monkeypatch.setattr("api.views.printer_status", boom)

    response = auth(user).get(f"/api/v1/printers/{printer.pk}/status/")

    assert response.status_code == 200
    body = response.json()
    assert body["online"] is False
    assert body["state"] == "offline"
    assert "cannot reach Moonraker" in body["error"]
    assert body["cfs_slots"] == []


def test_printer_status_keeps_status_when_cfs_fails(printer, user, monkeypatch):
    monkeypatch.setattr(
        "api.views.printer_status",
        lambda _printer: PrinterStatus(online=True, state=PrinterState.IDLE),
    )

    def cfs_boom(_printer):
        raise PrinterConnectionError("CFS WebSocket unreachable")

    monkeypatch.setattr("api.views.printer_cfs_slots", cfs_boom)

    response = auth(user).get(f"/api/v1/printers/{printer.pk}/status/")

    body = response.json()
    assert body["online"] is True
    assert body["state"] == "idle"
    assert body["cfs_slots"] == []
    assert "CFS WebSocket unreachable" in body["cfs_error"]


def test_printer_detail_is_404_for_a_missing_printer(user):
    assert auth(user).get("/api/v1/printers/999999/").status_code == 404


# ---------------------------------------------------------------------------
# Staff-only management
# ---------------------------------------------------------------------------


def test_printer_list_shows_host_to_staff_but_never_the_api_key(printer, staff):
    row = auth(staff).get("/api/v1/printers/").json()["results"][0]

    assert row["host"] == "10.0.0.7"
    assert "api_key" not in row


def test_printer_create_requires_staff(user):
    payload = {"name": "New", "backend": "moonraker", "host": "printer.local"}

    assert auth(user).post("/api/v1/printers/", payload, format="json").status_code == 403


def test_staff_can_create_a_printer_without_echoing_the_api_key(staff):
    response = auth(staff).post(
        "/api/v1/printers/",
        {
            "name": "Moonraker",
            "backend": "moonraker",
            "host": "printer.local",
            "api_key": "secret-token",
        },
        format="json",
    )

    assert response.status_code == 201
    body = response.json()
    assert body["host"] == "printer.local"
    assert "api_key" not in body
    stored = Printer.objects.get(pk=body["id"])
    assert stored.api_key == "secret-token"


def test_printer_create_rejects_an_unknown_backend(staff):
    response = auth(staff).post(
        "/api/v1/printers/", {"name": "X", "backend": "nope"}, format="json"
    )

    assert response.status_code == 400
    assert "backend" in response.json()


def test_staff_patch_keeps_the_api_key_when_left_empty(printer, staff):
    response = auth(staff).patch(
        f"/api/v1/printers/{printer.pk}/",
        {"name": "Renamed", "api_key": ""},
        format="json",
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"
    printer.refresh_from_db()
    assert printer.api_key == "super-secret"


def test_staff_patch_updates_the_api_key(printer, staff):
    response = auth(staff).patch(
        f"/api/v1/printers/{printer.pk}/", {"api_key": "rotated"}, format="json"
    )

    assert response.status_code == 200
    printer.refresh_from_db()
    assert printer.api_key == "rotated"


def test_printer_delete_deactivates_instead_of_removing(printer, staff):
    response = auth(staff).delete(f"/api/v1/printers/{printer.pk}/")

    assert response.status_code == 204
    printer.refresh_from_db()
    assert printer.is_active is False
    assert Printer.objects.filter(pk=printer.pk).exists()


def test_printer_write_is_denied_for_non_staff(printer, user):
    assert (
        auth(user)
        .patch(f"/api/v1/printers/{printer.pk}/", {"name": "X"}, format="json")
        .status_code
        == 403
    )
    assert auth(user).delete(f"/api/v1/printers/{printer.pk}/").status_code == 403
