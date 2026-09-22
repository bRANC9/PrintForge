"""Placeholder adapter for non-implemented printers (terv.md 13. fejezet).

``BambuBackend`` exists so ``Printer.backend`` has a stable registry key and
:mod:`printers.factory` can dispatch, but it is **intentionally not implemented
yet**. Every operation raises
:class:`~printers.base.PrinterProtocolNotImplementedError`; it never fakes
success.

When the protocol is implemented it should follow the Creality K2 / OctoPrint
pattern: a replaceable transport/client behind the adapter, plus tests that
inject a fake. Bambu's LAN mode is MQTT + FTPS and is unofficial and
firmware-specific; the Cloud path requires a Bambu account.
"""

from __future__ import annotations

from typing import NoReturn

from .base import PrinterBackend, PrinterProtocolNotImplementedError, PrinterStatus

__all__ = ["BambuBackend"]


class BambuBackend(PrinterBackend):
    """Bambu Lab adapter (TODO, see module docstring)."""

    name = "bambu"
    todo = (
        "BambuBackend is not implemented. TODO(printer-integration): add a Bambu "
        "LAN (MQTT + FTPS) or Cloud transport behind this adapter; never log the "
        "access code or token."
    )

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
