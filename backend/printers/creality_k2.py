"""Creality K2 Pro adapter (terv.md 13. fejezet).

Reality check -- what is actually true about the K2 Pro
-------------------------------------------------------

There is **no official, open, documented local control API** for the K2 Pro.
The machine runs a *locked* Creality OS on top of Klipper, and the pieces that
look like a local API are gated or unofficial:

* Klipper is the firmware underneath, and a Moonraker/Fluidd stack does ship on
  the device (Moonraker on port ``7125``, Fluidd on ``4408``). It is **not
  exposed by default**: Creality's Moonraker config restricts clients via
  ``trusted_clients``/``[authorization]`` and the config tree is root-owned and
  locked, so an arbitrary LAN host is refused until the operator unlocks the
  printer. Moonraker also knows nothing about the CFS.
* The CFS (Creality Filament System) is a proprietary subsystem that speaks its
  own protocol over RS-485 internally and a Creality WebSocket on port ``9999``
  externally. Community projects (k2-websocket-re, CFSync, ha-creality-lan)
  reverse-engineered it; it is **unofficial and firmware-version dependent**.
* The officially supported path is Creality Cloud (HTTP + MQTT) or CrealityPrint
  LAN control. Both require a Creality account/app and are not self-hosted, so
  they are not a fit for this project's core.

Consequently this adapter does **not** implement a protocol. It defines
:class:`K2Transport`, a replaceable boundary that mirrors the high-level
operations, and an :class:`UnconfiguredK2Transport` that refuses every call with
a precise TODO. The rest of the app never depends on the K2: the queue and the
factory treat it like any other backend, and a failed/refused operation marks
the job ``FAILED`` instead of faking success. When someone is ready to invest in
the protocol, they implement one transport (and pin the validated firmware
version) without touching ``base``/``services``/``factory``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import NoReturn

from .base import (
    CfsSlot,
    PrinterBackend,
    PrinterProtocolNotImplementedError,
    PrinterStatus,
)

__all__ = [
    "K2_FLUIDD_PORT",
    "K2_MOONRAKER_PORT",
    "K2_WEBSOCKET_PORT",
    "CrealityK2Backend",
    "K2Transport",
    "UnconfiguredK2Transport",
]

#: Known local ports on a K2 series device (documented for the TODO below).
K2_MOONRAKER_PORT = 7125  # Klipper HTTP API -- gated by trusted_clients
K2_FLUIDD_PORT = 4408  # bundled Fluidd web UI
K2_WEBSOCKET_PORT = 9999  # Creality-proprietary live status + CFS WebSocket

#: The precise TODO surfaced whenever the K2 protocol is needed. Kept as a
#: module constant so tests and callers can assert on it verbatim.
K2_PROTOCOL_TODO = (
    "Creality K2 Pro has no public local API. TODO(printer-integration): "
    "implement a K2Transport and pin the firmware version it was validated on. "
    "Candidates: (a) Moonraker on port 7125 for status/upload/start/cancel -- "
    "requires the client IP in Moonraker's trusted_clients/authorization, which "
    "Creality's locked config does not expose by default, and it does NOT expose "
    "CFS; (b) the proprietary Creality WebSocket on port 9999 for live status "
    "and CFS slot data (reverse-engineered, unofficial, firmware-specific); "
    "(c) Creality Cloud MQTT/HTTP (requires a Creality account, not "
    "self-hosted). No shell, never log credentials/tokens."
)


class K2Transport(ABC):
    """Replaceable wire-protocol boundary for the K2 Pro.

    This is the only place K2-specific communication may live. Subclasses (e.g.
    a future ``MoonrakerK2Transport`` or ``WebSocketK2Transport``) translate the
    operations below into the real protocol; the adapter and the rest of the app
    stay protocol-agnostic. ``host`` is the printer's LAN address.
    """

    def __init__(self, host: str, *, timeout: float = 5.0) -> None:
        self.host = host
        self.timeout = timeout

    @classmethod
    def from_printer(cls, printer: object) -> K2Transport:
        """Build the default transport for ``printer``.

        Today this is :class:`UnconfiguredK2Transport`; once a real transport
        exists this is the single switch-over point.
        """
        return UnconfiguredK2Transport(host=getattr(printer, "host", "") or "")

    @abstractmethod
    def fetch_status(self) -> PrinterStatus:
        """Return the device status snapshot."""

    @abstractmethod
    def upload_file(self, gcode_bytes: bytes, filename: str) -> str:
        """Store ``gcode_bytes`` on the device and return its remote filename."""

    @abstractmethod
    def start_print(self, filename: str) -> None:
        """Start the uploaded ``filename``."""

    @abstractmethod
    def cancel_print(self) -> None:
        """Abort the active print."""

    @abstractmethod
    def fetch_cfs_slots(self) -> list[CfsSlot]:
        """Return the CFS slots, or ``[]`` when no CFS is attached."""


class UnconfiguredK2Transport(K2Transport):
    """Default K2 transport: refuses every operation with a precise TODO.

    It never fakes success. The app keeps working because the K2 is just one
    registry entry and the queue never assumes a live printer.
    """

    def _fail(self) -> NoReturn:
        raise PrinterProtocolNotImplementedError(K2_PROTOCOL_TODO)

    def fetch_status(self) -> PrinterStatus:
        self._fail()

    def upload_file(self, gcode_bytes: bytes, filename: str) -> str:
        self._fail()

    def start_print(self, filename: str) -> None:
        self._fail()

    def cancel_print(self) -> None:
        self._fail()

    def fetch_cfs_slots(self) -> list[CfsSlot]:
        self._fail()


class CrealityK2Backend(PrinterBackend):
    """Printer backend for the Creality K2 series.

    The backend is a thin, protocol-agnostic shell over a :class:`K2Transport`.
    Tests inject a fake transport; production currently gets
    :class:`UnconfiguredK2Transport`, which raises
    :class:`~printers.base.PrinterProtocolNotImplementedError` until the real
    protocol is implemented.
    """

    name = "creality_k2"

    def __init__(self, printer: object, *, transport: K2Transport | None = None) -> None:
        super().__init__(printer)
        self._transport = transport if transport is not None else K2Transport.from_printer(printer)

    @property
    def transport(self) -> K2Transport:
        return self._transport

    def status(self) -> PrinterStatus:
        return self._transport.fetch_status()

    def upload(self, gcode_bytes: bytes, filename: str) -> str:
        if not gcode_bytes:
            raise ValueError("gcode_bytes must not be empty")
        if not filename:
            raise ValueError("filename must not be empty")
        return self._transport.upload_file(gcode_bytes, filename)

    def start(self, filename: str) -> None:
        if not filename:
            raise ValueError("filename must not be empty")
        self._transport.start_print(filename)

    def cancel(self) -> None:
        self._transport.cancel_print()

    def cfs_slots(self) -> list[CfsSlot]:
        return self._transport.fetch_cfs_slots()
