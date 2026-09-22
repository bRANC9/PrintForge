"""Tests for the Moonraker adapter -- no network, the opener is faked.

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

from printers.base import (
    PrinterConnectionError,
    PrinterState,
    PrinterStatus,
)
from printers.moonraker import (
    MoonrakerBackend,
    MoonrakerClient,
    MoonrakerError,
    moonraker_base_url,
    status_from_objects,
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
    """Records requests and returns a canned body (or raises)."""

    def __init__(self, body: bytes = b"{}", *, error: Exception | None = None) -> None:
        self.body = body
        self.error = error
        self.requests: list[tuple[object, float | None]] = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return FakeResponse(self.body)


def _printer(host: str = "printer.local", api_key: str = "") -> SimpleNamespace:
    return SimpleNamespace(host=host, api_key=api_key, backend="moonraker")


# ---------------------------------------------------------------------------
# Base URL resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("printer.local", "http://printer.local:7125"),
        ("printer.local:7126", "http://printer.local:7126"),
        ("http://printer.local:7125", "http://printer.local:7125"),
        ("http://printer.local", "http://printer.local:7125"),
        ("  10.0.0.5  ", "http://10.0.0.5:7125"),
        ("", ""),
    ],
)
def test_moonraker_base_url(host, expected):
    assert moonraker_base_url(host) == expected


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------


def test_status_from_objects_maps_printing():
    status = status_from_objects(
        {
            "status": {
                "print_stats": {"state": "printing", "filename": "cube.gcode", "message": "ok"},
                "virtual_sdcard": {"progress": 0.42},
            }
        }
    )

    assert status.online is True
    assert status.state == PrinterState.PRINTING
    assert status.current_filename == "cube.gcode"
    assert status.progress == 0.42
    assert status.message == "ok"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("standby", PrinterState.IDLE),
        ("complete", PrinterState.IDLE),
        ("cancelled", PrinterState.IDLE),
        ("paused", PrinterState.PAUSED),
        ("error", PrinterState.ERROR),
        ("weird", PrinterState.UNKNOWN),
    ],
)
def test_status_state_mapping(raw, expected):
    assert status_from_objects({"status": {"print_stats": {"state": raw}}}).state == expected


def test_status_falls_back_to_display_status_progress():
    status = status_from_objects({"status": {"display_status": {"progress": 0.75}}})
    assert status.progress == 0.75


def test_status_tolerates_a_missing_payload():
    status = status_from_objects({})
    assert isinstance(status, PrinterStatus)
    assert status.online is True
    assert status.state == PrinterState.UNKNOWN
    assert status.progress is None


# ---------------------------------------------------------------------------
# HTTP operations
# ---------------------------------------------------------------------------


def test_query_objects_builds_a_valueless_query_and_returns_result():
    opener = FakeOpener(json.dumps({"result": {"status": {"print_stats": {}}}}).encode())
    client = MoonrakerClient("http://printer.local:7125", opener=opener)

    result = client.query_objects("print_stats", "virtual_sdcard")

    request = opener.requests[0][0]
    assert request.full_url == (
        "http://printer.local:7125/printer/objects/query?print_stats&virtual_sdcard"
    )
    assert request.get_method() == "GET"
    assert result == {"status": {"print_stats": {}}}


def test_api_key_is_sent_only_when_configured():
    opener = FakeOpener(b"{}")
    MoonrakerClient("http://printer.local:7125", api_key="secret", opener=opener).query_objects(
        "print_stats"
    )
    request = opener.requests[0][0]
    assert request.get_header("X-api-key") == "secret"

    opener2 = FakeOpener(b"{}")
    MoonrakerClient("http://printer.local:7125", opener=opener2).query_objects("print_stats")
    assert opener2.requests[0][0].get_header("X-api-key") is None


def test_upload_posts_multipart_and_returns_remote_path():
    opener = FakeOpener(
        json.dumps({"result": {"item": {"path": "cube.gcode", "root": "gcodes"}}}).encode()
    )
    client = MoonrakerClient("http://printer.local:7125", opener=opener)

    remote = client.upload_file(b"G28\n", "local.gcode")

    request = opener.requests[0][0]
    assert request.full_url == "http://printer.local:7125/server/files/upload"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type").startswith("multipart/form-data; boundary=")
    assert b'filename="local.gcode"' in request.data
    assert b"G28\n" in request.data
    assert remote == "cube.gcode"


def test_upload_falls_back_to_the_local_filename_without_an_item():
    opener = FakeOpener(json.dumps({"result": {}}).encode())
    client = MoonrakerClient("http://printer.local:7125", opener=opener)

    assert client.upload_file(b"G28\n", "local.gcode") == "local.gcode"


def test_start_and_cancel_use_post():
    opener = FakeOpener(b"{}")
    client = MoonrakerClient("http://printer.local:7125", opener=opener)

    client.start_print("cube.gcode")
    client.cancel_print()

    start_request = opener.requests[0][0]
    assert start_request.full_url == (
        "http://printer.local:7125/printer/print/start?filename=cube.gcode"
    )
    assert start_request.get_method() == "POST"
    assert opener.requests[1][0].full_url == "http://printer.local:7125/printer/print/cancel"
    assert opener.requests[1][0].get_method() == "POST"


# ---------------------------------------------------------------------------
# Errors -- never fake success
# ---------------------------------------------------------------------------


def test_blank_host_raises_connection_error():
    client = MoonrakerClient("", opener=FakeOpener(b"{}"))
    with pytest.raises(PrinterConnectionError):
        client.query_objects("print_stats")


def test_unreachable_host_raises_connection_error():
    opener = FakeOpener(error=urllib.error.URLError("connection refused"))
    client = MoonrakerClient("http://printer.local:7125", opener=opener)
    with pytest.raises(PrinterConnectionError):
        client.query_objects("print_stats")


def test_http_error_raises_moonraker_error():
    error = urllib.error.HTTPError(
        "http://printer.local:7125", 500, "boom", {}, io.BytesIO(b"klippy error")
    )
    client = MoonrakerClient("http://printer.local:7125", opener=FakeOpener(error=error))
    with pytest.raises(MoonrakerError):
        client.query_objects("print_stats")


def test_invalid_json_raises_moonraker_error():
    client = MoonrakerClient("http://printer.local:7125", opener=FakeOpener(b"not json"))
    with pytest.raises(MoonrakerError):
        client.query_objects("print_stats")


def test_error_payload_raises_moonraker_error():
    body = json.dumps({"error": {"message": "klippy not ready"}}).encode()
    client = MoonrakerClient("http://printer.local:7125", opener=FakeOpener(body))
    with pytest.raises(MoonrakerError):
        client.query_objects("print_stats")


def test_upload_and_start_validate_inputs():
    client = MoonrakerClient("http://printer.local:7125", opener=FakeOpener(b"{}"))
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
    body = json.dumps({"result": {"status": {"print_stats": {"state": "paused"}}}}).encode()
    backend = MoonrakerBackend(
        _printer(), client=MoonrakerClient("http://x:7125", opener=FakeOpener(body))
    )

    assert backend.status().state == PrinterState.PAUSED


def test_backend_cfs_slots_is_empty():
    backend = MoonrakerBackend(_printer())
    assert backend.cfs_slots() == []


def test_backend_name_is_stable():
    assert MoonrakerBackend.name == "moonraker"
