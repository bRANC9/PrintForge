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
  printer. The CFS is exposed as the Klipper ``box`` object
  (``GET /printer/objects/query?box``); the payload shape is firmware-dependent
  (see :mod:`printers.k2_box`).
* The CFS (Creality Filament System) is a proprietary subsystem that speaks its
  own protocol over RS-485 internally and a Creality WebSocket on port ``9999``
  externally. Community projects (k2-websocket-re, CFSync, ha-creality-lan)
  reverse-engineered it; it is **unofficial and firmware-version dependent**.
* The officially supported path is Creality Cloud (HTTP + MQTT) or CrealityPrint
  LAN control. Both require a Creality account/app and are not self-hosted, so
  they are not a fit for this project's core.

Consequently the adapter ships a **best-effort, opt-in** transport:

* :class:`MoonrakerK2Transport` uses the on-device Moonraker (port ``7125``) for
  status/upload/start/cancel -- exactly the documented Klipper API. For the CFS
  it prefers Moonraker's ``[box]`` object query and falls back to the
  proprietary WebSocket (port ``9999``) when that query fails or reports no
  slots. Both paths are unofficial/gated and firmware-dependent; they never fake
  success, so an unreachable or locked printer raises
  :class:`~printers.base.PrinterConnectionError` and the job is marked
  ``FAILED``.
* :class:`UnconfiguredK2Transport` remains the explicit "no protocol" fallback
  (kept for tests and for deployments that want to refuse rather than try).

The rest of the app never depends on the K2: the queue and the factory treat it
like any other backend. Pin the validated firmware version in the deployment
notes when you roll this out.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import NoReturn

from .base import (
    CfsSlot,
    PrinterBackend,
    PrinterBackendError,
    PrinterProtocolNotImplementedError,
    PrinterStatus,
)
from .k2_box import slots_from_box_object
from .k2_websocket import K2_WEBSOCKET_PORT, read_cfs_slots
from .moonraker import MoonrakerClient, moonraker_base_url, status_from_objects

__all__ = [
    "K2_FLUIDD_PORT",
    "K2_MOONRAKER_PORT",
    "K2_WEBSOCKET_PORT",
    "CrealityK2Backend",
    "K2Transport",
    "MoonrakerK2Transport",
    "UnconfiguredK2Transport",
]

#: Known local ports on a K2 series device.
K2_MOONRAKER_PORT = 7125  # Klipper HTTP API -- gated by trusted_clients
K2_FLUIDD_PORT = 4408  # bundled Fluidd web UI
# K2_WEBSOCKET_PORT (9999) is defined in ``k2_websocket`` and re-exported above.

#: The precise TODO surfaced by :class:`UnconfiguredK2Transport`. Kept as a
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

        This is :class:`MoonrakerK2Transport`: the K2 ships a Moonraker stack for
        the print operations and the proprietary WebSocket for CFS. A blank host
        or a locked/offline printer raises at call time rather than faking
        success.
        """
        return MoonrakerK2Transport(
            host=getattr(printer, "host", "") or "",
            api_key=getattr(printer, "api_key", "") or "",
        )

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


class MoonrakerK2Transport(K2Transport):
    """K2 transport using the on-device Moonraker + the CFS WebSocket fallback.

    Print operations (status/upload/start/cancel) go through
    :class:`~printers.moonraker.MoonrakerClient` on port ``7125``. The CFS is
    read from Moonraker's ``[box]`` object first (the primary source), falling
    back to the proprietary WebSocket on port ``9999`` when that query fails or
    reports no slots. Both the client and the WebSocket reader are injectable so
    tests never touch the network. Nothing is faked: an unreachable printer
    raises :class:`~printers.base.PrinterConnectionError`.
    """

    def __init__(
        self,
        host: str,
        *,
        api_key: str = "",
        timeout: float = 5.0,
        client: MoonrakerClient | None = None,
        cfs_reader: Callable[..., list[CfsSlot]] | None = None,
        cfs_port: int = K2_WEBSOCKET_PORT,
    ) -> None:
        super().__init__(host, timeout=timeout)
        self._client = client or MoonrakerClient(
            moonraker_base_url(host), api_key=api_key, timeout=timeout
        )
        self._cfs_reader = cfs_reader or read_cfs_slots
        self._cfs_port = cfs_port

    @property
    def client(self) -> MoonrakerClient:
        return self._client

    def fetch_status(self) -> PrinterStatus:
        return status_from_objects(self._client.query_objects())

    def upload_file(self, gcode_bytes: bytes, filename: str) -> str:
        return self._client.upload_file(gcode_bytes, filename)

    def start_print(self, filename: str) -> None:
        self._client.start_print(filename)

    def cancel_print(self) -> None:
        self._client.cancel_print()

    def fetch_cfs_slots(self) -> list[CfsSlot]:
        """Return the CFS slots, preferring Moonraker's ``[box]`` object.

        The stock K2 Moonraker exposes the CFS as the Klipper ``box`` object
        (``GET /printer/objects/query?box``), which is the primary source. When
        that query fails (locked/older firmware, no ``box`` module) or reports
        no slots, fall back to the proprietary port-9999 WebSocket reader.
        Nothing is fabricated: if both paths fail, the WebSocket error
        propagates so the queue can mark the job ``FAILED``.
        """
        box_slots = self._box_slots()
        if box_slots:
            return box_slots
        return self._cfs_reader(self.host, port=self._cfs_port, timeout=self.timeout)

    def _box_slots(self) -> list[CfsSlot]:
        """CFS slots from Moonraker's ``box`` object, or ``[]`` when unavailable."""
        try:
            return slots_from_box_object(self._client.query_box())
        except PrinterBackendError:
            # The box object is optional; a failure here must not hide the
            # WebSocket fallback. ``slots_from_box_object`` never raises, so the
            # only errors caught are the client's connection/protocol errors.
            return []


class CrealityK2Backend(PrinterBackend):
    """Printer backend for the Creality K2 series.

    The backend is a thin, protocol-agnostic shell over a :class:`K2Transport`.
    Tests inject a fake transport; production gets
    :class:`MoonrakerK2Transport` (Moonraker on 7125 + CFS WebSocket on 9999),
    which raises :class:`~printers.base.PrinterConnectionError` when the printer
    is unreachable or locked instead of faking success.
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
