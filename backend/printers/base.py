"""Backend-agnostic printer interface (terv.md 13-14. fejezet).

The core application (queue, API, MCP tools) only ever talks to a
:class:`PrinterBackend`. Concrete adapters -- CrealityK2 first, then Moonraker,
OctoPrint and Bambu -- live in sibling modules and are resolved by
:mod:`printers.factory`. Nothing here knows about a wire protocol, a host or a
credential, so a new printer brand can be added without touching the core.

Contract
--------

* :meth:`PrinterBackend.status` -- online/offline + current print state.
* :meth:`PrinterBackend.upload` -- store ``gcode_bytes`` and return the
  *remote* filename the printer now knows the file by.
* :meth:`PrinterBackend.start` -- start a previously uploaded file.
* :meth:`PrinterBackend.cancel` -- abort the active print.
* :meth:`PrinterBackend.cfs_slots` -- CFS (multi-filament) slots; the default
  is an empty list because not every printer has a CFS.

Adapters must **never fake success**. When a protocol is unknown or a host is
unreachable they raise :class:`PrinterBackendError` (or
:class:`PrinterProtocolNotImplementedError`) so the queue can mark the job
``FAILED`` instead of pretending it printed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "CfsSlot",
    "PrinterBackend",
    "PrinterBackendError",
    "PrinterConnectionError",
    "PrinterProtocolNotImplementedError",
    "PrinterState",
    "PrinterStatus",
]


class PrinterBackendError(Exception):
    """Base class for every printer backend failure."""


class PrinterConnectionError(PrinterBackendError):
    """The printer host could not be reached or refused the connection."""


class PrinterProtocolNotImplementedError(PrinterBackendError, NotImplementedError):
    """The adapter does not (yet) know how to talk to this firmware.

    Subclasses :class:`NotImplementedError` so callers may catch either the
    specific backend error or the generic Python one. It exists so an adapter
    can *refuse* to act rather than fabricate a successful result.
    """


class PrinterState(StrEnum):
    """Canonical printer states shared by every backend.

    Adapters map their protocol-specific states onto these values; a backend
    that cannot classify the device state returns :attr:`UNKNOWN`.
    """

    OFFLINE = "offline"
    IDLE = "idle"
    PRINTING = "printing"
    PAUSED = "paused"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CfsSlot:
    """One slot of a Creality Filament System (CFS) unit (terv.md 13. fejezet).

    ``color`` is whatever the printer reports -- a human name (``"Black"``) or a
    hex string (``"#1a1a1a"``) -- and is intentionally not parsed here.
    ``empty`` is the authoritative "no spool" flag; an empty slot may still
    carry stale ``material``/``name`` values, so callers should check
    ``empty`` first.
    """

    index: int
    material: str = ""
    brand: str = ""
    name: str = ""
    color: str = ""
    state: str = ""
    empty: bool = True


@dataclass(frozen=True)
class PrinterStatus:
    """A point-in-time snapshot of a printer.

    ``progress`` is a ``0.0``-``1.0`` fraction when the firmware reports one,
    otherwise ``None``. ``raw`` holds the device payload for diagnostics; it may
    contain credentials or tokens and **must never be logged**.
    """

    online: bool
    state: str = PrinterState.UNKNOWN
    current_filename: str | None = None
    progress: float | None = None
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class PrinterBackend(ABC):
    """Interface every printer backend must implement.

    ``printer`` is the ``printers.Printer`` registry row (host, backend key,
    ...). Adapters read configuration from it but must not mutate it.
    """

    #: Stable registry key (matches ``Printer.backend``).
    name = "base"

    def __init__(self, printer: Any) -> None:
        self.printer = printer

    @property
    def host(self) -> str:
        """Configured host, or an empty string when unset."""
        return getattr(self.printer, "host", "") or ""

    @abstractmethod
    def status(self) -> PrinterStatus:
        """Return the current printer status snapshot."""

    @abstractmethod
    def upload(self, gcode_bytes: bytes, filename: str) -> str:
        """Upload ``gcode_bytes`` and return the remote filename to ``start()``."""

    @abstractmethod
    def start(self, filename: str) -> None:
        """Start the previously uploaded ``filename``."""

    @abstractmethod
    def cancel(self) -> None:
        """Cancel the active print."""

    def cfs_slots(self) -> list[CfsSlot]:
        """Return the CFS filament slots.

        Defaults to an empty list: CFS is Creality-specific, so adapters for
        printers without it simply inherit this behaviour.
        """
        return []
