"""Placeholder adapters for non-Creality printers (terv.md 13. fejezet).

These classes exist so ``Printer.backend`` has a stable registry key and
:mod:`printers.factory` can dispatch, but they are **intentionally not
implemented yet**. Every operation raises
:class:`~printers.base.PrinterProtocolNotImplementedError`; none of them fakes
success.

When a protocol is implemented it should follow the Creality K2 pattern: a
replaceable transport object behind the adapter, plus tests that inject a fake
transport. Candidate first steps:

* ``MoonrakerBackend`` -- Klipper's documented HTTP API on port ``7125``
  (``/server/files/upload``, ``/printer/print/start``, ``/printer/objects/query``).
* ``OctoPrintBackend`` -- OctoPrint REST API with an application API key
  (``/api/files/local``, ``/api/job``).
* ``BambuBackend`` -- Bambu Lab's MQTT + FTPS LAN mode, or Bambu Cloud; the LAN
  protocol is unofficial and firmware-specific.
"""

from __future__ import annotations

from typing import NoReturn

from .base import PrinterBackend, PrinterProtocolNotImplementedError, PrinterStatus

__all__ = ["BambuBackend", "MoonrakerBackend", "OctoPrintBackend"]


class _UnimplementedBackend(PrinterBackend):
    """Base for registry-only adapters that refuse every operation."""

    #: Human-readable protocol note surfaced in the error message.
    todo = "This printer backend is not implemented yet."

    def _fail(self) -> NoReturn:
        raise PrinterProtocolNotImplementedError(self.todo)

    def status(self) -> PrinterStatus:
        self._fail()

    def upload(self, gcode_bytes: bytes, filename: str) -> str:
        self._fail()

    def start(self, filename: str) -> None:
        self._fail()

    def cancel(self) -> None:
        self._fail()

    def cfs_slots(self):
        self._fail()


class MoonrakerBackend(_UnimplementedBackend):
    """Klipper/Moonraker adapter (TODO, see module docstring)."""

    name = "moonraker"
    todo = (
        "MoonrakerBackend is not implemented. TODO(printer-integration): add a "
        "Moonraker transport (HTTP + optional API key, port 7125) behind this "
        "adapter; never log the API key. CFS slots are not part of Moonraker."
    )


class OctoPrintBackend(_UnimplementedBackend):
    """OctoPrint adapter (TODO, see module docstring)."""

    name = "octoprint"
    todo = (
        "OctoPrintBackend is not implemented. TODO(printer-integration): add an "
        "OctoPrint REST transport (X-Api-Key header) behind this adapter; never "
        "log the API key."
    )


class BambuBackend(_UnimplementedBackend):
    """Bambu Lab adapter (TODO, see module docstring)."""

    name = "bambu"
    todo = (
        "BambuBackend is not implemented. TODO(printer-integration): add a Bambu "
        "LAN (MQTT + FTPS) or Cloud transport behind this adapter; never log the "
        "access code or token."
    )
