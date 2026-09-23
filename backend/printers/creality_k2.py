"""Creality K2 adapter (terv.md 13. fejezet).

Current reality -- K2 series protocol
-------------------------------------

The K2 series runs Klipper with a stock Moonraker stack (HTTP on port ``7125``,
Fluidd on ``4408``). Creality still ships **no official, open, documented local
control API**, but the Klipper/Moonraker surface is present on the device and is
what this adapter uses:

* Print operations (status / upload / start / cancel) go through Moonraker --
  the documented Klipper API.
* The CFS (Creality Filament System) is exposed by Creality's ``[box]`` Klipper
  module. The **primary** CFS source is the standard object query
  ``GET /printer/objects/query?box`` (see :mod:`printers.k2_box`), which needs no
  bespoke framing.
* When the ``[box]`` query is unavailable or reports no slots, the adapter falls
  back to the reverse-engineered Creality WebSocket on port ``9999``
  (:mod:`printers.k2_websocket`; community ``k2-websocket-re`` /
  ``Creality-K2-websocket``). That protocol is **unofficial and
  firmware-dependent**.

Moonraker on the K2 is often **gated**: Creality's config restricts LAN clients
via ``trusted_clients`` / ``[authorization]`` and the config tree is root-owned,
so an arbitrary host is refused until the operator adds it to
``trusted_clients`` or supplies an API key. The officially supported Creality
Cloud (HTTP + MQTT) / CrealityPrint LAN path needs a Creality account and is
**out of scope** for this self-hosted core.

Design
------

* :class:`MoonrakerK2Transport` is the default produced by
  :meth:`K2Transport.from_printer`: Moonraker for print operations, the ``[box]``
  object as the primary CFS source and the port-9999 WebSocket as fallback. It
  never fakes success -- an unreachable or locked printer raises
  :class:`~printers.base.PrinterConnectionError` and the job is marked
  ``FAILED``.
* :class:`UnconfiguredK2Transport` is an explicit refuse-rather-than-fake
  fallback for callers that want no network attempt at all.
* The K2-specific wire protocol lives only behind :class:`K2Transport`, so the
  core (queue, API, factory) never depends on it; the K2 is just another registry
  entry.

Operational notes and the per-device validation checklist live in
:data:`K2_OPERATIONAL_NOTES`: pin the validated firmware, unlock Moonraker for
the app host, and never log credentials or tokens. No shell is used anywhere in
this adapter.
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
    "K2_OPERATIONAL_NOTES",
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

#: Operational notes for a real K2 deployment. This is **not** a TODO: the
#: Moonraker transport is implemented. It records what an operator must validate
#: per device so the adapter can reach the printer, and is surfaced verbatim by
#: :class:`UnconfiguredK2Transport`.
K2_OPERATIONAL_NOTES = (
    "K2 deployment checklist: (1) add the app host to Moonraker's "
    "trusted_clients, or set Printer.api_key for [authorization]; (2) pin the "
    "validated firmware version per device -- the [box] payload and the port-9999 "
    "CFS schema are firmware-dependent; (3) CFS is read from the Klipper [box] "
    "object (/printer/objects/query?box) with the port-9999 WebSocket as "
    "fallback; (4) the official Creality Cloud path is out of scope. Never log "
    "credentials or tokens; no shell."
)


class K2Transport(ABC):
    """Replaceable wire-protocol boundary for the K2 series.

    This is the only place K2-specific communication may live. The concrete
    :class:`MoonrakerK2Transport` translates the operations below into Moonraker
    plus the CFS ``[box]``/WebSocket protocol; the adapter and the rest of the app
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
    """Explicit refuse-rather-than-fake transport (not the default).

    :meth:`K2Transport.from_printer` returns :class:`MoonrakerK2Transport`, so
    production never uses this. It is kept for callers and tests that want a
    transport which raises for every operation instead of touching the network;
    it never fabricates a successful result. The message carries the deployment
    checklist from :data:`K2_OPERATIONAL_NOTES`.
    """

    def _fail(self) -> NoReturn:
        raise PrinterProtocolNotImplementedError(
            "No K2 transport configured; set Printer.host so "
            "K2Transport.from_printer builds a MoonrakerK2Transport. "
            f"{K2_OPERATIONAL_NOTES}"
        )

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
