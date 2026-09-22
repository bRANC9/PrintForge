"""Tests for the Creality K2 adapter -- no network, transport is faked.

The real K2 protocol is unknown (see ``printers/creality_k2.py``), so the
default transport must *refuse* every operation. These tests also verify that
the adapter correctly delegates to an injected transport, which is how a future
real transport will be exercised.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from printers.base import CfsSlot, PrinterProtocolNotImplementedError, PrinterState, PrinterStatus
from printers.creality_k2 import (
    K2_PROTOCOL_TODO,
    CrealityK2Backend,
    K2Transport,
    MoonrakerK2Transport,
    UnconfiguredK2Transport,
)


def _printer(host: str = "k2.local", api_key: str = "") -> SimpleNamespace:
    return SimpleNamespace(host=host, backend="creality_k2", api_key=api_key)


class FakeTransport(K2Transport):
    """In-memory transport recording every call the adapter makes."""

    def __init__(
        self,
        *,
        status: PrinterStatus | None = None,
        slots: list[CfsSlot] | None = None,
        remote_filename: str = "remote.gcode",
    ) -> None:
        super().__init__(host="fake")
        self._status = status or PrinterStatus(online=True, state=PrinterState.IDLE)
        self._slots = slots or []
        self._remote_filename = remote_filename
        self.calls: list[object] = []

    def fetch_status(self) -> PrinterStatus:
        self.calls.append("status")
        return self._status

    def upload_file(self, gcode_bytes: bytes, filename: str) -> str:
        self.calls.append(("upload", gcode_bytes, filename))
        return self._remote_filename

    def start_print(self, filename: str) -> None:
        self.calls.append(("start", filename))

    def cancel_print(self) -> None:
        self.calls.append("cancel")

    def fetch_cfs_slots(self) -> list[CfsSlot]:
        self.calls.append("cfs")
        return list(self._slots)


# ---------------------------------------------------------------------------
# Unconfigured transport -- must never fake success
# ---------------------------------------------------------------------------


def test_unconfigured_transport_refuses_status_with_todo():
    backend = CrealityK2Backend(_printer(), transport=UnconfiguredK2Transport(host="k2.local"))
    with pytest.raises(PrinterProtocolNotImplementedError) as excinfo:
        backend.status()
    assert "TODO(printer-integration)" in str(excinfo.value)


def test_unconfigured_transport_refuses_every_operation():
    backend = CrealityK2Backend(_printer(), transport=UnconfiguredK2Transport(host="k2.local"))
    for call in (
        lambda: backend.upload(b"gcode", "model.gcode"),
        lambda: backend.start("model.gcode"),
        lambda: backend.cancel(),
        lambda: backend.cfs_slots(),
    ):
        with pytest.raises(PrinterProtocolNotImplementedError):
            call()


def test_unconfigured_transport_exposes_todo_constant():
    transport = UnconfiguredK2Transport(host="k2.local")
    assert transport.host == "k2.local"
    assert "K2 Pro" in K2_PROTOCOL_TODO
    with pytest.raises(NotImplementedError):
        transport.fetch_cfs_slots()


def test_from_printer_builds_a_moonraker_transport():
    transport = K2Transport.from_printer(_printer("10.0.0.7", api_key="secret"))

    assert isinstance(transport, MoonrakerK2Transport)
    assert transport.host == "10.0.0.7"
    assert transport.client.api_key == "secret"
    assert transport.client.base_url == "http://10.0.0.7:7125"


# ---------------------------------------------------------------------------
# Adapter delegation with an injected transport
# ---------------------------------------------------------------------------


def test_status_delegates_to_transport():
    expected = PrinterStatus(online=True, state=PrinterState.PRINTING, progress=0.42)
    transport = FakeTransport(status=expected)
    backend = CrealityK2Backend(_printer(), transport=transport)

    assert backend.status() is expected
    assert transport.calls == ["status"]


def test_upload_delegates_and_returns_remote_filename():
    transport = FakeTransport(remote_filename="job_42.gcode")
    backend = CrealityK2Backend(_printer(), transport=transport)

    remote = backend.upload(b"G28\n", "local.gcode")

    assert remote == "job_42.gcode"
    assert transport.calls == [("upload", b"G28\n", "local.gcode")]


def test_start_and_cancel_delegate():
    transport = FakeTransport()
    backend = CrealityK2Backend(_printer(), transport=transport)

    backend.start("job_42.gcode")
    backend.cancel()

    assert transport.calls == [("start", "job_42.gcode"), "cancel"]


def test_cfs_slots_delegates_and_returns_dataclasses():
    slots = [
        CfsSlot(index=0, material="Hyper PLA", brand="Creality", color="Black", empty=False),
        CfsSlot(index=1, empty=True),
    ]
    transport = FakeTransport(slots=slots)
    backend = CrealityK2Backend(_printer(), transport=transport)

    assert backend.cfs_slots() == slots
    assert transport.calls == ["cfs"]


def test_upload_rejects_empty_input_without_touching_transport():
    transport = FakeTransport()
    backend = CrealityK2Backend(_printer(), transport=transport)

    with pytest.raises(ValueError):
        backend.upload(b"", "local.gcode")
    with pytest.raises(ValueError):
        backend.upload(b"gcode", "")

    assert transport.calls == []


def test_start_rejects_empty_filename():
    transport = FakeTransport()
    backend = CrealityK2Backend(_printer(), transport=transport)

    with pytest.raises(ValueError):
        backend.start("")

    assert transport.calls == []


def test_backend_name_is_stable():
    assert CrealityK2Backend.name == "creality_k2"


# ---------------------------------------------------------------------------
# MoonrakerK2Transport -- Moonraker for print ops, WebSocket for CFS
# ---------------------------------------------------------------------------


class FakeMoonrakerClient:
    """Records calls and returns canned Moonraker results."""

    def __init__(self, *, objects=None, remote="remote.gcode"):
        self._objects = objects or {}
        self._remote = remote
        self.calls: list[object] = []

    def query_objects(self, *objects):
        self.calls.append(("query", objects))
        return self._objects

    def upload_file(self, gcode_bytes, filename):
        self.calls.append(("upload", gcode_bytes, filename))
        return self._remote

    def start_print(self, filename):
        self.calls.append(("start", filename))

    def cancel_print(self):
        self.calls.append("cancel")


def test_moonraker_transport_maps_status():
    client = FakeMoonrakerClient(
        objects={
            "status": {
                "print_stats": {"state": "printing", "filename": "cube.gcode"},
                "virtual_sdcard": {"progress": 0.5},
            }
        }
    )
    transport = MoonrakerK2Transport("k2.local", client=client)

    status = transport.fetch_status()

    assert status.online is True
    assert status.state == PrinterState.PRINTING
    assert status.current_filename == "cube.gcode"
    assert status.progress == 0.5


def test_moonraker_transport_upload_start_cancel_delegate():
    client = FakeMoonrakerClient(remote="job.gcode")
    transport = MoonrakerK2Transport("k2.local", client=client)

    assert transport.upload_file(b"G28\n", "local.gcode") == "job.gcode"
    transport.start_print("job.gcode")
    transport.cancel_print()

    assert client.calls == [
        ("upload", b"G28\n", "local.gcode"),
        ("start", "job.gcode"),
        "cancel",
    ]


def test_moonraker_transport_reads_cfs_from_the_injected_reader():
    slots = [CfsSlot(index=0, material="PLA", empty=False)]
    seen: dict[str, object] = {}

    def fake_reader(host, *, port, timeout):
        seen.update(host=host, port=port, timeout=timeout)
        return slots

    transport = MoonrakerK2Transport("k2.local", timeout=2.0, cfs_reader=fake_reader)

    assert transport.fetch_cfs_slots() == slots
    assert seen == {"host": "k2.local", "port": 9999, "timeout": 2.0}


def test_creality_backend_uses_moonraker_transport_by_default():
    backend = CrealityK2Backend(_printer("10.0.0.9"))

    assert isinstance(backend.transport, MoonrakerK2Transport)
    assert backend.transport.host == "10.0.0.9"
