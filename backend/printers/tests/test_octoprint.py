"""Tests for the OctoPrint adapter -- no network, the opener is faked.

The ``urllib`` opener is injected, so these tests exercise the real URL/multipart
construction, the response parsing, the status mapping and every error path
without touching a printer.
"""

from __future__ import annotations

import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from printers.base import PrinterConnectionError, PrinterState, PrinterStatus
from printers.octoprint import (
    OctoPrintBackend,
    OctoPrintClient,
    OctoPrintError,
    octoprint_base_url,
    status_from_job,
)


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc) -> bool:
        return False


class FakeOpener:
    def __init__(self, body: bytes = b"{}", *, error: Exception | None = None) -> None:
        self.body = body
        self.error = error
        self.requests: list[tuple[object, float | None]] = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return FakeResponse(self.body)


def _printer(host: str = "octopi.local", api_key: str = "") -> SimpleNamespace:
    return SimpleNamespace(host=host, api_key=api_key, backend="octoprint")


# ---------------------------------------------------------------------------
# Base URL resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("octopi.local", "http://octopi.local:5000"),
        ("octopi.local:80", "http://octopi.local:80"),
        ("http://octopi.local:5000", "http://octopi.local:5000"),
        ("http://octopi.local", "http://octopi.local:5000"),
        ("  ", ""),
    ],
)
def test_octoprint_base_url(host, expected):
    assert octoprint_base_url(host) == expected


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------


def test_status_from_job_maps_printing():
    status = status_from_job(
        {
            "state": "Printing",
            "job": {"file": {"name": "cube.gcode", "path": "cube.gcode"}},
            "progress": {"completion": 42.5},
        }
    )

    assert status.online is True
    assert status.state == PrinterState.PRINTING
    assert status.current_filename == "cube.gcode"
    assert status.progress == 0.425
    assert status.message == "Printing"


@pytest.mark.parametrize(
    ("raw", "expected", "online"),
    [
        ("Operational", PrinterState.IDLE, True),
        ("Starting", PrinterState.PRINTING, True),
        ("Finishing", PrinterState.PRINTING, True),
        ("Paused", PrinterState.PAUSED, True),
        ("Offline", PrinterState.OFFLINE, False),
        ("Closed", PrinterState.OFFLINE, False),
        ("Error: heater failed", PrinterState.ERROR, True),
        ("Weird", PrinterState.UNKNOWN, True),
    ],
)
def test_status_state_mapping(raw, expected, online):
    status = status_from_job({"state": raw})
    assert status.state == expected
    assert status.online is online


def test_status_tolerates_a_missing_payload():
    status = status_from_job({})
    assert isinstance(status, PrinterStatus)
    assert status.state == PrinterState.UNKNOWN
    assert status.progress is None


# ---------------------------------------------------------------------------
# HTTP operations
# ---------------------------------------------------------------------------


def test_fetch_job_gets_api_job():
    opener = FakeOpener(json.dumps({"state": "Operational"}).encode())
    client = OctoPrintClient("http://octopi.local:5000", opener=opener)

    result = client.fetch_job()

    request = opener.requests[0][0]
    assert request.full_url == "http://octopi.local:5000/api/job"
    assert request.get_method() == "GET"
    assert result == {"state": "Operational"}


def test_api_key_is_sent_only_when_configured():
    opener = FakeOpener(b"{}")
    OctoPrintClient("http://octopi.local:5000", api_key="secret", opener=opener).fetch_job()
    assert opener.requests[0][0].get_header("X-api-key") == "secret"

    opener2 = FakeOpener(b"{}")
    OctoPrintClient("http://octopi.local:5000", opener=opener2).fetch_job()
    assert opener2.requests[0][0].get_header("X-api-key") is None


def test_upload_posts_multipart_and_returns_remote_path():
    opener = FakeOpener(
        json.dumps({"files": {"local": {"name": "cube.gcode", "path": "cube.gcode"}}}).encode()
    )
    client = OctoPrintClient("http://octopi.local:5000", opener=opener)

    remote = client.upload_file(b"G28\n", "local.gcode")

    request = opener.requests[0][0]
    assert request.full_url == "http://octopi.local:5000/api/files/local"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type").startswith("multipart/form-data; boundary=")
    assert b'filename="local.gcode"' in request.data
    assert b"G28\n" in request.data
    assert remote == "cube.gcode"


def test_upload_falls_back_to_the_local_filename_without_a_file_entry():
    opener = FakeOpener(json.dumps({"done": True}).encode())
    client = OctoPrintClient("http://octopi.local:5000", opener=opener)

    assert client.upload_file(b"G28\n", "local.gcode") == "local.gcode"


def test_start_selects_and_prints_the_file():
    opener = FakeOpener(b"{}")
    client = OctoPrintClient("http://octopi.local:5000", opener=opener)

    client.start_print("my cube.gcode")

    request = opener.requests[0][0]
    assert request.full_url == "http://octopi.local:5000/api/files/local/my%20cube.gcode"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {"command": "select", "print": True}


def test_cancel_posts_the_job_command():
    opener = FakeOpener(b"{}")
    client = OctoPrintClient("http://octopi.local:5000", opener=opener)

    client.cancel_print()

    request = opener.requests[0][0]
    assert request.full_url == "http://octopi.local:5000/api/job"
    assert json.loads(request.data) == {"command": "cancel"}


# ---------------------------------------------------------------------------
# Errors -- never fake success
# ---------------------------------------------------------------------------


def test_blank_host_raises_connection_error():
    client = OctoPrintClient("", opener=FakeOpener(b"{}"))
    with pytest.raises(PrinterConnectionError):
        client.fetch_job()


def test_unreachable_host_raises_connection_error():
    opener = FakeOpener(error=urllib.error.URLError("connection refused"))
    client = OctoPrintClient("http://octopi.local:5000", opener=opener)
    with pytest.raises(PrinterConnectionError):
        client.fetch_job()


def test_http_error_raises_octoprint_error():
    error = urllib.error.HTTPError(
        "http://octopi.local:5000", 403, "forbidden", {}, io.BytesIO(b"invalid api key")
    )
    client = OctoPrintClient("http://octopi.local:5000", opener=FakeOpener(error=error))
    with pytest.raises(OctoPrintError):
        client.fetch_job()


def test_error_payload_raises_octoprint_error():
    client = OctoPrintClient(
        "http://octopi.local:5000",
        opener=FakeOpener(json.dumps({"error": "Printer is not operational"}).encode()),
    )
    with pytest.raises(OctoPrintError):
        client.cancel_print()


def test_upload_and_start_validate_inputs():
    client = OctoPrintClient("http://octopi.local:5000", opener=FakeOpener(b"{}"))
    with pytest.raises(ValueError):
        client.upload_file(b"", "local.gcode")
    with pytest.raises(ValueError):
        client.upload_file(b"G28\n", "")
    with pytest.raises(ValueError):
        client.start_print("")


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


def test_backend_status_delegates_to_the_client():
    body = json.dumps({"state": "Paused"}).encode()
    backend = OctoPrintBackend(
        _printer(), client=OctoPrintClient("http://x:5000", opener=FakeOpener(body))
    )

    assert backend.status().state == PrinterState.PAUSED


def test_backend_cfs_slots_is_empty():
    assert OctoPrintBackend(_printer()).cfs_slots() == []


def test_backend_name_is_stable():
    assert OctoPrintBackend.name == "octoprint"
