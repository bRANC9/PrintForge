"""Tests for the printer backend registry / factory -- no network, no DB."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from printers.base import PrinterBackend, PrinterStatus
from printers.creality_k2 import CrealityK2Backend
from printers.factory import (
    BACKEND_REGISTRY,
    UnknownPrinterBackendError,
    get_printer_backend,
    register_backend,
    supported_backends,
)
from printers.moonraker import MoonrakerBackend
from printers.octoprint import OctoPrintBackend
from printers.stubs import BambuBackend


def _printer(backend: str, host: str = "printer.local") -> SimpleNamespace:
    return SimpleNamespace(backend=backend, host=host)


class _CustomBackend(PrinterBackend):
    name = "custom"

    def status(self) -> PrinterStatus:
        return PrinterStatus(online=False)

    def upload(self, gcode_bytes: bytes, filename: str) -> str:
        return filename

    def start(self, filename: str) -> None:
        return None

    def cancel(self) -> None:
        return None


@pytest.mark.parametrize(
    ("key", "expected_cls"),
    [
        ("creality_k2", CrealityK2Backend),
        ("k2", CrealityK2Backend),
        ("creality", CrealityK2Backend),
        ("moonraker", MoonrakerBackend),
        ("octoprint", OctoPrintBackend),
        ("bambu", BambuBackend),
    ],
)
def test_dispatch_by_backend_key(key, expected_cls):
    printer = _printer(key)
    backend = get_printer_backend(printer)
    assert isinstance(backend, expected_cls)
    assert backend.printer is printer


def test_dispatch_is_case_insensitive_and_strips_whitespace():
    backend = get_printer_backend(_printer("  Creality_K2  "))
    assert isinstance(backend, CrealityK2Backend)


def test_unknown_backend_raises_with_supported_list():
    with pytest.raises(UnknownPrinterBackendError) as excinfo:
        get_printer_backend(_printer("prusa"))
    assert "prusa" in str(excinfo.value)
    assert "creality_k2" in str(excinfo.value)


def test_blank_backend_raises():
    with pytest.raises(UnknownPrinterBackendError):
        get_printer_backend(_printer(""))


def test_supported_backends_includes_builtins():
    assert {"creality_k2", "moonraker", "octoprint", "bambu"} <= set(supported_backends())


def test_register_backend_adds_and_dispatches(monkeypatch):
    monkeypatch.setitem(BACKEND_REGISTRY, "custom", _CustomBackend)

    backend = get_printer_backend(_printer("custom"))

    assert isinstance(backend, _CustomBackend)


def test_register_backend_rejects_empty_name():
    with pytest.raises(ValueError):
        register_backend("  ", _CustomBackend)


def test_register_backend_rejects_non_backend_class():
    with pytest.raises(TypeError):
        register_backend("bad", object)  # type: ignore[arg-type]


def test_stub_backends_refuse_operations():
    backend = BambuBackend(_printer("bambu"))
    with pytest.raises(NotImplementedError):
        backend.status()


def test_moonraker_and_octoprint_backends_are_implemented():
    assert isinstance(get_printer_backend(_printer("moonraker")), MoonrakerBackend)
    assert isinstance(get_printer_backend(_printer("octoprint")), OctoPrintBackend)
