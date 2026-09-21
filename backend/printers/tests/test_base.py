"""Tests for the protocol-agnostic printer interface (``printers.base``)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from printers.base import (
    CfsSlot,
    PrinterBackend,
    PrinterBackendError,
    PrinterConnectionError,
    PrinterProtocolNotImplementedError,
    PrinterState,
    PrinterStatus,
)


class _DummyBackend(PrinterBackend):
    name = "dummy"

    def status(self) -> PrinterStatus:
        return PrinterStatus(online=True, state=PrinterState.IDLE)

    def upload(self, gcode_bytes: bytes, filename: str) -> str:
        return filename

    def start(self, filename: str) -> None:
        return None

    def cancel(self) -> None:
        return None


def test_cfs_slots_defaults_to_empty_for_non_cfs_printers():
    backend = _DummyBackend(SimpleNamespace(host="printer.local"))
    assert backend.cfs_slots() == []


def test_host_property_reads_printer_and_tolerates_missing():
    assert _DummyBackend(SimpleNamespace(host="192.168.1.50")).host == "192.168.1.50"
    assert _DummyBackend(SimpleNamespace()).host == ""


def test_cfs_slot_defaults_and_shape():
    slot = CfsSlot(index=0)
    assert slot.material == ""
    assert slot.brand == ""
    assert slot.name == ""
    assert slot.color == ""
    assert slot.state == ""
    assert slot.empty is True


def test_cfs_slot_is_frozen():
    slot = CfsSlot(index=1, material="PLA", color="Black", empty=False)
    with pytest.raises(FrozenInstanceError):
        slot.material = "PETG"  # type: ignore[misc]


def test_printer_status_defaults():
    status = PrinterStatus(online=False)
    assert status.state == PrinterState.UNKNOWN
    assert status.current_filename is None
    assert status.progress is None
    assert status.message == ""
    assert status.raw == {}


def test_printer_state_values_are_stable():
    assert {state.value for state in PrinterState} == {
        "offline",
        "idle",
        "printing",
        "paused",
        "error",
        "unknown",
    }


def test_protocol_not_implemented_is_a_backend_error_and_notimplemented():
    assert issubclass(PrinterProtocolNotImplementedError, PrinterBackendError)
    assert issubclass(PrinterProtocolNotImplementedError, NotImplementedError)


def test_connection_error_is_a_backend_error():
    assert issubclass(PrinterConnectionError, PrinterBackendError)
