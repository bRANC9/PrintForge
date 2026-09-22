"""OctoPrint adapter (terv.md 13. fejezet).

OctoPrint exposes a documented REST API (default port ``5000``). This module
implements the four operations the queue needs -- status, upload, start, cancel
-- over :mod:`printers._http`'s shared JSON/multipart plumbing.

Auth
----

OctoPrint requires an *application API key* for the API. It comes from
``Printer.api_key`` and is sent as the ``X-Api-Key`` header. It is **never
logged** and never included in :class:`~printers.base.PrinterStatus.raw`.

Endpoints
---------

* ``GET  /api/job``                       -- state, current file, progress
* ``POST /api/files/local`` (multipart)   -- upload a G-code file
* ``POST /api/files/local/{path}``        -- ``{"command": "select", "print": true}``
* ``POST /api/job``                       -- ``{"command": "cancel"}``

OctoPrint has no Creality Filament System, so :meth:`OctoPrintBackend.cfs_slots`
returns ``[]``.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from ._http import DEFAULT_TIMEOUT_SEC, JsonHttpClient, base_url_for, multipart_body
from .base import PrinterBackend, PrinterBackendError, PrinterState, PrinterStatus

__all__ = [
    "DEFAULT_OCTOPRINT_PORT",
    "OctoPrintBackend",
    "OctoPrintClient",
    "OctoPrintError",
    "octoprint_base_url",
]

#: OctoPrint's own HTTP port (OctoPi often fronts it on 80 via haproxy).
DEFAULT_OCTOPRINT_PORT = 5000

#: OctoPrint ``job.state`` -> canonical :class:`PrinterState`.
_STATE_MAP: dict[str, str] = {
    "operational": PrinterState.IDLE,
    "printing": PrinterState.PRINTING,
    "starting": PrinterState.PRINTING,
    "finishing": PrinterState.PRINTING,
    "cancelling": PrinterState.PRINTING,
    "pausing": PrinterState.PRINTING,
    "paused": PrinterState.PAUSED,
    "offline": PrinterState.OFFLINE,
    "closed": PrinterState.OFFLINE,
    "error": PrinterState.ERROR,
}


class OctoPrintError(PrinterBackendError):
    """OctoPrint answered with an error or an unexpected payload."""


def octoprint_base_url(host: str, *, default_port: int = DEFAULT_OCTOPRINT_PORT) -> str:
    """Normalise ``host`` into an OctoPrint base URL (see :func:`base_url_for`)."""
    return base_url_for(host, default_port=default_port)


class OctoPrintClient(JsonHttpClient):
    """OctoPrint HTTP client; see :class:`~printers._http.JsonHttpClient`."""

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
            error_cls=OctoPrintError,
            label="OctoPrint",
        )

    # -- public operations --------------------------------------------------

    def fetch_job(self) -> dict[str, Any]:
        """Return the ``GET /api/job`` payload."""
        return self.request_json("/api/job")

    def upload_file(self, gcode_bytes: bytes, filename: str) -> str:
        """Upload ``gcode_bytes`` into the local file list and return its path."""
        if not gcode_bytes:
            raise ValueError("gcode_bytes must not be empty")
        if not filename:
            raise ValueError("filename must not be empty")
        body, content_type = multipart_body(
            {
                "file": (filename, gcode_bytes),
                "select": "false",
                "print": "false",
            }
        )
        payload = self.request_json(
            "/api/files/local",
            method="POST",
            data=body,
            headers={"Content-Type": content_type},
        )
        files = payload.get("files")
        local = files.get("local") if isinstance(files, dict) else None
        if isinstance(local, dict):
            remote = local.get("path") or local.get("name")
            if remote:
                return str(remote)
        return filename

    def start_print(self, filename: str) -> None:
        """Select ``filename`` and start printing it in one call."""
        if not filename:
            raise ValueError("filename must not be empty")
        path = "/api/files/local/" + urllib.parse.quote(filename, safe="")
        self.request_json(path, method="POST", json_body={"command": "select", "print": True})

    def cancel_print(self) -> None:
        """Cancel the active print."""
        self.request_json("/api/job", method="POST", json_body={"command": "cancel"})


def status_from_job(payload: dict[str, Any]) -> PrinterStatus:
    """Map an OctoPrint ``GET /api/job`` payload onto :class:`PrinterStatus`."""
    state_text = str(payload.get("state") or "").strip()
    key = state_text.lower()
    if key.startswith("error"):
        state = PrinterState.ERROR
    else:
        state = _STATE_MAP.get(key, PrinterState.UNKNOWN)

    job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
    file_info = job.get("file") if isinstance(job.get("file"), dict) else {}
    filename = file_info.get("name") or file_info.get("path") or None

    progress_info = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
    completion = progress_info.get("completion")
    try:
        progress = float(completion) / 100.0 if completion is not None else None
    except (TypeError, ValueError):
        progress = None

    return PrinterStatus(
        online=state != PrinterState.OFFLINE,
        state=state,
        current_filename=str(filename) if filename else None,
        progress=progress,
        message=state_text,
        raw={"job": job, "progress": progress_info},
    )


class OctoPrintBackend(PrinterBackend):
    """Printer backend speaking OctoPrint's REST API.

    The backend is a thin shell over an :class:`OctoPrintClient`; ``client`` is
    injectable so tests never touch the network.
    """

    name = "octoprint"

    def __init__(self, printer: Any, *, client: OctoPrintClient | None = None) -> None:
        super().__init__(printer)
        self._client = client or OctoPrintClient(
            octoprint_base_url(self.host),
            api_key=getattr(printer, "api_key", "") or "",
        )

    @property
    def client(self) -> OctoPrintClient:
        return self._client

    def status(self) -> PrinterStatus:
        return status_from_job(self._client.fetch_job())

    def upload(self, gcode_bytes: bytes, filename: str) -> str:
        return self._client.upload_file(gcode_bytes, filename)

    def start(self, filename: str) -> None:
        self._client.start_print(filename)

    def cancel(self) -> None:
        self._client.cancel_print()

    def cfs_slots(self) -> list:
        """OctoPrint has no Creality Filament System; return no slots."""
        return []
