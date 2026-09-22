"""Moonraker (Klipper) adapter (terv.md 13. fejezet).

Moonraker is Klipper's documented local HTTP API (default port ``7125``). This
module implements the four operations the queue needs -- status, upload, start,
cancel -- plus a :class:`MoonrakerClient` that the Creality K2 transport reuses
for its own Moonraker stack.

The HTTP/JSON/multipart plumbing is shared with the other backends in
:mod:`printers._http`; only the Moonraker payloads are mapped here.

Auth
----

A Moonraker may allow the LAN via ``trusted_clients`` (no credential) or require
an API key through ``[authorization]``. The key comes from
``Printer.api_key`` and is sent as the ``X-Api-Key`` header. It is **never
logged** and never included in :class:`~printers.base.PrinterStatus.raw`.

CFS
---

Moonraker knows nothing about the Creality Filament System, so
:meth:`MoonrakerBackend.cfs_slots` returns ``[]`` (the base contract for a
printer without a CFS). The K2 reads its CFS from the proprietary WebSocket
instead (see :mod:`printers.k2_websocket`).
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from ._http import DEFAULT_TIMEOUT_SEC, JsonHttpClient, base_url_for, multipart_body
from .base import (
    PrinterBackend,
    PrinterBackendError,
    PrinterState,
    PrinterStatus,
)

__all__ = [
    "DEFAULT_MOONRAKER_PORT",
    "DEFAULT_TIMEOUT_SEC",
    "MoonrakerBackend",
    "MoonrakerClient",
    "MoonrakerError",
    "moonraker_base_url",
    "status_from_objects",
]

#: Moonraker's default HTTP port.
DEFAULT_MOONRAKER_PORT = 7125

#: Object names queried for a status snapshot.
STATUS_OBJECTS = ("print_stats", "virtual_sdcard", "display_status")

#: Moonraker ``print_stats.state`` -> canonical :class:`PrinterState`.
_STATE_MAP: dict[str, str] = {
    "standby": PrinterState.IDLE,
    "printing": PrinterState.PRINTING,
    "paused": PrinterState.PAUSED,
    "complete": PrinterState.IDLE,
    "cancelled": PrinterState.IDLE,
    "error": PrinterState.ERROR,
}


class MoonrakerError(PrinterBackendError):
    """Moonraker answered with an error or an unexpected payload."""


def moonraker_base_url(host: str, *, default_port: int = DEFAULT_MOONRAKER_PORT) -> str:
    """Normalise ``host`` into a Moonraker base URL (see :func:`base_url_for`)."""
    return base_url_for(host, default_port=default_port)


class MoonrakerClient(JsonHttpClient):
    """Moonraker HTTP client; see :class:`~printers._http.JsonHttpClient`."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT_SEC,
        opener=None,
    ) -> None:
        super().__init__(
            base_url,
            api_key=api_key,
            timeout=timeout,
            opener=opener,
            error_cls=MoonrakerError,
            label="Moonraker",
        )

    # -- public operations --------------------------------------------------

    def query_objects(self, *objects: str) -> dict[str, Any]:
        """Return ``result`` of ``/printer/objects/query`` for ``objects``."""
        names = objects or STATUS_OBJECTS
        # Moonraker wants valueless query keys (``?print_stats&virtual_sdcard``).
        query = "&".join(urllib.parse.quote(name, safe="") for name in names)
        return self.request_json(f"/printer/objects/query?{query}").get("result", {})

    def upload_file(self, gcode_bytes: bytes, filename: str) -> str:
        """Upload ``gcode_bytes`` under ``gcodes/`` and return its remote path."""
        if not gcode_bytes:
            raise ValueError("gcode_bytes must not be empty")
        if not filename:
            raise ValueError("filename must not be empty")
        body, content_type = multipart_body({"root": "gcodes", "file": (filename, gcode_bytes)})
        result = self.request_json(
            "/server/files/upload",
            method="POST",
            data=body,
            headers={"Content-Type": content_type},
        ).get("result", {})
        item = result.get("item") if isinstance(result, dict) else None
        if isinstance(item, dict):
            remote = item.get("path") or item.get("name")
            if remote:
                return str(remote)
        return filename

    def start_print(self, filename: str) -> None:
        """Start the previously uploaded ``filename`` (relative to ``gcodes``)."""
        if not filename:
            raise ValueError("filename must not be empty")
        self.request_json("/printer/print/start", method="POST", params={"filename": filename})

    def cancel_print(self) -> None:
        """Abort the active print."""
        self.request_json("/printer/print/cancel", method="POST")


def status_from_objects(result: dict[str, Any]) -> PrinterStatus:
    """Map a ``/printer/objects/query`` ``result`` onto :class:`PrinterStatus`."""
    status = result.get("status") if isinstance(result, dict) else None
    status = status if isinstance(status, dict) else {}
    print_stats = status.get("print_stats") if isinstance(status.get("print_stats"), dict) else {}
    virtual_sdcard = (
        status.get("virtual_sdcard") if isinstance(status.get("virtual_sdcard"), dict) else {}
    )
    display_status = (
        status.get("display_status") if isinstance(status.get("display_status"), dict) else {}
    )

    raw_state = str(print_stats.get("state") or "").strip().lower()
    state = _STATE_MAP.get(raw_state, PrinterState.UNKNOWN)

    progress = virtual_sdcard.get("progress")
    if progress is None:
        progress = display_status.get("progress")
    try:
        progress = float(progress) if progress is not None else None
    except (TypeError, ValueError):
        progress = None

    filename = print_stats.get("filename") or None
    message = str(print_stats.get("message") or "")

    return PrinterStatus(
        online=True,
        state=state,
        current_filename=str(filename) if filename else None,
        progress=progress,
        message=message,
        raw={"print_stats": print_stats, "virtual_sdcard": virtual_sdcard},
    )


class MoonrakerBackend(PrinterBackend):
    """Printer backend speaking Moonraker's HTTP API.

    The backend is a thin shell over a :class:`MoonrakerClient`; ``client`` is
    injectable so tests never touch the network. There is no CFS on a plain
    Moonraker printer, so :meth:`cfs_slots` returns ``[]``.
    """

    name = "moonraker"

    def __init__(self, printer: Any, *, client: MoonrakerClient | None = None) -> None:
        super().__init__(printer)
        self._client = client or MoonrakerClient(
            moonraker_base_url(self.host),
            api_key=getattr(printer, "api_key", "") or "",
        )

    @property
    def client(self) -> MoonrakerClient:
        return self._client

    def status(self) -> PrinterStatus:
        return status_from_objects(self._client.query_objects())

    def upload(self, gcode_bytes: bytes, filename: str) -> str:
        return self._client.upload_file(gcode_bytes, filename)

    def start(self, filename: str) -> None:
        self._client.start_print(filename)

    def cancel(self) -> None:
        self._client.cancel_print()

    def cfs_slots(self) -> list:
        """Moonraker has no Creality Filament System; return no slots."""
        return []
