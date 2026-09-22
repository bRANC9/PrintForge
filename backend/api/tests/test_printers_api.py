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
