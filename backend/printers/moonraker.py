"""Moonraker (Klipper) adapter (terv.md 13. fejezet).

Moonraker is Klipper's documented local HTTP API (default port ``7125``). This
module implements the four operations the queue needs -- status, upload, start,
cancel -- plus a :class:`MoonrakerClient` that the Creality K2 transport reuses
for its own Moonraker stack.

Only the standard library is used (``urllib.request``), matching
:mod:`agents.llm.ollama`: the project gains no extra dependency.

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

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any

from .base import (
    PrinterBackend,
    PrinterBackendError,
    PrinterConnectionError,
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
DEFAULT_TIMEOUT_SEC = 10.0

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

#: Opener seam: ``(request, timeout=...) -> context-manager response``.
Opener = Callable[..., Any]


class MoonrakerError(PrinterBackendError):
    """Moonraker answered with an error or an unexpected payload."""


def moonraker_base_url(host: str, *, default_port: int = DEFAULT_MOONRAKER_PORT) -> str:
    """Normalise ``host`` into a Moonraker base URL.

    Accepts a bare host (``printer.local``), a host with port
    (``printer.local:7125``) or a full URL (``http://printer.local:7125``). A
    missing port falls back to ``default_port``; a missing scheme to ``http``.
    A blank host yields ``""`` so callers fail with a clear "not configured"
    error instead of connecting to ``:7125``.
    """
    host = (host or "").strip()
    if not host:
        return ""
    if "://" not in host:
        host = f"//{host}"
    parsed = urllib.parse.urlsplit(host)
    scheme = parsed.scheme or "http"
    netloc = parsed.netloc
    if not parsed.port:
        netloc = f"{netloc}:{default_port}"
    return f"{scheme}://{netloc}".rstrip("/")


def _multipart_body(fields: dict[str, Any]) -> tuple[bytes, str]:
    """Encode ``fields`` as a multipart/form-data body.

    A ``tuple`` value is treated as ``(filename, payload_bytes)``; anything else
    is sent as a plain text part.
    """
    boundary = f"----printforge{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode("ascii"))
        if isinstance(value, tuple):
            filename, payload = value
            parts.append(
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
            )
            parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
            parts.append(bytes(payload) + b"\r\n")
        else:
            parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            parts.append(str(value).encode("utf-8") + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class MoonrakerClient:
    """Thin, injectable HTTP client for one Moonraker instance.

    ``opener`` defaults to :func:`urllib.request.urlopen`; tests inject a fake
    with the same ``(request, timeout=...)`` signature, so no network is needed.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT_SEC,
        opener: Opener | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = str(api_key or "")
        self.timeout = float(timeout)
        self._opener: Opener = opener or urllib.request.urlopen

    # -- public operations --------------------------------------------------

    def query_objects(self, *objects: str) -> dict[str, Any]:
        """Return ``result`` of ``/printer/objects/query`` for ``objects``."""
        names = objects or STATUS_OBJECTS
        # Moonraker wants valueless query keys (``?print_stats&virtual_sdcard``).
        query = "&".join(urllib.parse.quote(name, safe="") for name in names)
        result = self._request(f"/printer/objects/query?{query}")
        return result if isinstance(result, dict) else {}

    def upload_file(self, gcode_bytes: bytes, filename: str) -> str:
        """Upload ``gcode_bytes`` under ``gcodes/`` and return its remote path."""
        if not gcode_bytes:
            raise ValueError("gcode_bytes must not be empty")
        if not filename:
            raise ValueError("filename must not be empty")
        body, content_type = _multipart_body(
            {
                "root": "gcodes",
                "file": (filename, gcode_bytes),
            }
        )
        result = self._request(
            "/server/files/upload",
            method="POST",
            data=body,
            headers={"Content-Type": content_type},
        )
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
        query = urllib.parse.urlencode({"filename": filename})
        self._request(f"/printer/print/start?{query}", method="POST")

    def cancel_print(self) -> None:
        """Abort the active print."""
        self._request("/printer/print/cancel", method="POST")

    # -- transport ----------------------------------------------------------

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if not self.base_url:
            raise PrinterConnectionError(
                "Moonraker host is not configured; set Printer.host (e.g. 192.168.1.50)"
            )
        url = f"{self.base_url}{path}"
        request_headers = {"Accept": "application/json"}
        if self.api_key:
            request_headers["X-Api-Key"] = self.api_key
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)

        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = _safe_body(exc)
            raise MoonrakerError(f"Moonraker HTTP {exc.code} on {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise PrinterConnectionError(f"Cannot reach Moonraker at {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise PrinterConnectionError(
                f"Moonraker request timed out after {self.timeout}s: {url}"
            ) from exc
        except OSError as exc:
            raise PrinterConnectionError(f"Moonraker request failed on {url}: {exc}") from exc

        if not body:
            return {}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MoonrakerError(f"Moonraker returned invalid JSON on {path}") from exc
        if not isinstance(payload, dict):
            raise MoonrakerError(f"Moonraker returned an unexpected payload on {path}")
        error = payload.get("error")
        if error is not None:
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise MoonrakerError(f"Moonraker error on {path}: {message}")
        return payload.get("result", {})


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


def _safe_body(exc: urllib.error.HTTPError) -> str:
    """Read at most 500 chars of an HTTP error body without raising."""
    try:
        return exc.read().decode("utf-8", "replace")[:500]
    except Exception:  # noqa: BLE001 - diagnostics must never mask the error
        return ""
