"""Shared stdlib HTTP plumbing for the printer backends (terv.md 13. fejezet).

Moonraker and OctoPrint both speak JSON over HTTP with an optional
``X-Api-Key`` header and multipart uploads. This module holds that shared
machinery -- request/JSON handling, error translation and a multipart encoder --
so each adapter only has to map its own payloads.

Only the standard library is used (``urllib.request``), matching
:mod:`agents.llm.ollama`: no extra dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any

from .base import PrinterBackendError, PrinterConnectionError

__all__ = ["DEFAULT_TIMEOUT_SEC", "JsonHttpClient", "base_url_for", "multipart_body"]

DEFAULT_TIMEOUT_SEC = 10.0

#: Opener seam: ``(request, timeout=...) -> context-manager response``.
Opener = Callable[..., Any]


def base_url_for(host: str, *, default_port: int, scheme: str = "http") -> str:
    """Normalise ``host`` into a base URL with a default port.

    Accepts a bare host (``printer.local``), a host with port
    (``printer.local:5000``) or a full URL (``http://printer.local:5000``). A
    missing port falls back to ``default_port``; a missing scheme to ``scheme``.
    A blank host yields ``""`` so callers fail with a clear "not configured"
    error instead of connecting to a default port on no host.
    """
    host = (host or "").strip()
    if not host:
        return ""
    if "://" not in host:
        host = f"//{host}"
    parsed = urllib.parse.urlsplit(host)
    resolved_scheme = parsed.scheme or scheme
    netloc = parsed.netloc
    if not parsed.port:
        netloc = f"{netloc}:{default_port}"
    return f"{resolved_scheme}://{netloc}".rstrip("/")


def multipart_body(fields: dict[str, Any]) -> tuple[bytes, str]:
    """Encode ``fields`` as a multipart/form-data body.

    A ``tuple`` value is treated as ``(filename, payload_bytes)``; anything else
    is sent as a plain text part. Returns ``(body, content_type)``.
    """
    boundary = f"----printforge{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        if isinstance(value, tuple):
            filename, payload = value
            parts.append(
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
            )
            parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
            parts.append(bytes(payload) + b"\r\n")
        else:
            parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            parts.append(str(value).encode() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class JsonHttpClient:
    """Minimal JSON HTTP client with an optional API key.

    ``opener`` defaults to :func:`urllib.request.urlopen`; tests inject a fake
    with the same ``(request, timeout=...)`` signature, so no network is needed.
    Connection problems raise :class:`~printers.base.PrinterConnectionError`;
    HTTP/protocol errors raise ``error_cls`` (the adapter's own error type).
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT_SEC,
        opener: Opener | None = None,
        error_cls: type[PrinterBackendError] = PrinterBackendError,
        label: str = "printer",
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = str(api_key or "")
        self.timeout = float(timeout)
        self._opener: Opener = opener or urllib.request.urlopen
        self._error_cls = error_cls
        self._label = label

    def request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Perform a request and return the decoded JSON object.

        ``json_body`` is serialised as JSON (and sets ``Content-Type``);
        ``data`` is sent verbatim (used for multipart). Both are mutually
        exclusive in practice. An empty body yields ``{}``.
        """
        if not self.base_url:
            raise PrinterConnectionError(f"{self._label} host is not configured; set Printer.host")
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        request_headers = {"Accept": "application/json"}
        if self.api_key:
            request_headers["X-Api-Key"] = self.api_key
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)

        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise self._error_cls(
                f"{self._label} HTTP {exc.code} on {path}: {_safe_body(exc)}"
            ) from exc
        except urllib.error.URLError as exc:
            raise PrinterConnectionError(
                f"Cannot reach {self._label} at {url}: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise PrinterConnectionError(
                f"{self._label} request timed out after {self.timeout}s: {url}"
            ) from exc
        except OSError as exc:
            raise PrinterConnectionError(f"{self._label} request failed on {url}: {exc}") from exc

        if not body:
            return {}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise self._error_cls(f"{self._label} returned invalid JSON on {path}") from exc
        if not isinstance(payload, dict):
            raise self._error_cls(f"{self._label} returned an unexpected payload on {path}")
        error = payload.get("error")
        if error is not None:
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise self._error_cls(f"{self._label} error on {path}: {message}")
        return payload


def _safe_body(exc: urllib.error.HTTPError) -> str:
    """Read at most 500 chars of an HTTP error body without raising."""
    try:
        return exc.read().decode("utf-8", "replace")[:500]
    except Exception:  # noqa: BLE001 - diagnostics must never mask the error
        return ""
