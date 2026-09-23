"""Best-effort web search backend for the Research agent (terv.md 7. fejezet).

The Research agent can look up real-world product and standard-part data. Next
to the local RAG knowledge base (``embeddings.services.retrieve``) it may query
a self-hosted **SearXNG** instance through its JSON API::

    GET {SEARXNG_BASE_URL}/search?q=<query>&format=json

The backend is deliberately **best-effort**: when the feature is disabled
(``SEARCH_BACKEND`` empty), the base URL is missing, the instance is unreachable
or it answers with unusable JSON, the call returns ``[]`` and logs a warning --
it never raises and never blocks a generation.

Network note: this runs in the Django/Celery worker, **not** in the OpenSCAD
sandbox, so outbound HTTP is allowed. Page fetching is still SSRF-guarded: only
``http``/``https`` URLs whose host resolves to a public IP are fetched, so a
model cannot make the worker probe ``localhost`` or the internal network.

Settings (resolved at call time through ``configuration.services.get_setting``,
so a runtime override applies without a restart):

* ``search_backend`` -- non-empty (e.g. ``"searxng"``) enables web search;
* ``searxng_base_url`` -- the instance base URL (only read when enabled).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

__all__ = [
    "DEFAULT_FETCH_RESULTS",
    "DEFAULT_MAX_RESULTS",
    "DEFAULT_TIMEOUT_SEC",
    "MAX_CONTENT_CHARS",
    "SEARXNG",
    "WebSearchBackend",
    "is_public_http_url",
]

logger = logging.getLogger(__name__)

#: ``search_backend`` value that enables the SearXNG-shaped JSON API.
SEARXNG = "searxng"

#: Bounded network timeout (seconds) for both search and page fetches.
DEFAULT_TIMEOUT_SEC = 8.0
#: Bounded number of search results returned/considered by default.
DEFAULT_MAX_RESULTS = 5
#: Bounded number of top results whose main text is fetched and extracted.
DEFAULT_FETCH_RESULTS = 2
#: Longest query accepted (sanity bound; the Planner query is already short).
MAX_QUERY_CHARS = 400
#: Longest extracted page text attached to a result (bounded payload).
MAX_CONTENT_CHARS = 3000
#: Longest title/snippet kept per result.
MAX_FIELD_CHARS = 2000

#: HTTP seam: ``(url, *, timeout) -> bytes | str``. Injected in tests so no
#: network is ever touched; the default uses the standard library.
HttpGetter = Callable[..., Any]


def _get_setting(name: str, default: Any = "") -> Any:
    """Read a runtime setting lazily; never raise for an unknown/missing value."""
    try:
        from configuration.services import get_setting

        return get_setting(name)
    except Exception:  # noqa: BLE001 - a bad setting must not break the import path
        logger.debug("Could not resolve setting %r", name)
        return default


def _default_http_get(url: str, *, timeout: float = DEFAULT_TIMEOUT_SEC) -> bytes:
    """Fetch *url* with the standard library and return the raw body bytes."""
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json, text/html;q=0.9",
            "User-Agent": "PrintForge-Research/1.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed http(s)
        return response.read()


def _trafilatura_extract(html: str) -> str | None:
    """Extract the main text of *html* with trafilatura (lazy, optional)."""
    from trafilatura import extract

    return extract(html)


def _clip(value: Any, limit: int = MAX_FIELD_CHARS) -> str:
    text = str(value).strip() if value is not None else ""
    return text[:limit]


def is_public_http_url(url: str) -> bool:
    """Return whether *url* is a public ``http``/``https`` URL safe to fetch.

    Blocks non-HTTP schemes, obvious local names and every host that resolves to
    a private, loopback, link-local, reserved, multicast or unspecified address
    (SSRF guard). A DNS failure returns ``False`` -- we only fetch what we can
    verify.
    """
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return False

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False

    if not infos:
        return False
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


class WebSearchBackend:
    """SearXNG-shaped JSON search with optional main-text extraction.

    Args:
        base_url: Override the ``searxng_base_url`` setting (mostly for tests).
        timeout: Network timeout in seconds for search and page fetches.
        max_results: Hard cap on the number of search results.
        fetch_results: How many top results to fetch/extract (0 disables it).
        http_get: Injectable HTTP seam; defaults to the stdlib getter.
        extract: Injectable HTML->text extractor; defaults to trafilatura.

    All entry points are best-effort and never raise.
    """

    name = SEARXNG

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        max_results: int = DEFAULT_MAX_RESULTS,
        fetch_results: int = DEFAULT_FETCH_RESULTS,
        http_get: HttpGetter | None = None,
        extract: Callable[[str], str | None] | None = None,
    ) -> None:
        self._base_url_override = base_url
        self._timeout = float(timeout)
        self._max_results = max(int(max_results), 1)
        self._fetch_results = max(int(fetch_results), 0)
        self._http_get = http_get or _default_http_get
        self._extract = extract or _trafilatura_extract

    # -- configuration ------------------------------------------------------

    def settings(self) -> tuple[str, str]:
        """Return the effective ``(backend, base_url)`` as resolved at call time."""
        backend = str(_get_setting("search_backend") or "").strip().lower()
        if self._base_url_override is not None:
            base_url = str(self._base_url_override).strip()
        else:
            base_url = str(_get_setting("searxng_base_url") or "").strip()
        return backend, base_url.rstrip("/")

    @property
    def enabled(self) -> bool:
        """Whether web search is configured (non-empty backend + base URL)."""
        backend, base_url = self.settings()
        return bool(backend) and bool(base_url)

    # -- public API ---------------------------------------------------------

    def search(self, query: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return ``[{"title", "url", "snippet"}]`` for *query* (never raises).

        When enabled and reachable, the top ``fetch_results`` results also gain a
        bounded ``"content"`` field with their extracted main text. A disabled
        feature, a transport error or unusable JSON all yield ``[]``.
        """
        try:
            return self._search(query, limit=limit)
        except Exception as exc:  # noqa: BLE001 - best-effort contract, must never raise
            logger.warning("Web search failed: %s", exc)
            return []

    # -- internals ----------------------------------------------------------

    def _search(self, query: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        cleaned = str(query or "").strip()[:MAX_QUERY_CHARS]
        if not cleaned or not self.enabled:
            return []

        wanted = self._max_results if limit is None else max(int(limit), 1)
        wanted = min(wanted, self._max_results)

        backend, base_url = self.settings()
        params = {
            "q": cleaned,
            "format": "json",
            "safesearch": "1",
            "language": "all",
        }
        url = f"{base_url}/search?{urllib.parse.urlencode(params)}"
        raw = self._http_get(url, timeout=self._timeout)
        payload = json.loads(_decode(raw))

        results = self._normalise(payload, wanted)
        self._fetch_pages(results)
        return results

    def _normalise(self, payload: Any, wanted: int) -> list[dict[str, Any]]:
        """Map a SearXNG JSON payload to the documented result shape."""
        items = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []

        results: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = _clip(item.get("url"))
            if not url:
                continue
            results.append(
                {
                    "title": _clip(item.get("title")),
                    "url": url,
                    "snippet": _clip(item.get("content") or item.get("snippet")),
                }
            )
            if len(results) >= wanted:
                break
        return results

    def _fetch_pages(self, results: list[dict[str, Any]]) -> None:
        """Best-effort main-text extraction for the top ``fetch_results`` URLs."""
        if self._fetch_results <= 0:
            return
        for result in results[: self._fetch_results]:
            url = result.get("url") or ""
            if not is_public_http_url(url):
                continue
            try:
                raw = self._http_get(url, timeout=self._timeout)
                text = self._extract(_decode(raw))
            except Exception as exc:  # noqa: BLE001 - a page fetch is optional
                logger.debug("Could not extract %s: %s", url, exc)
                continue
            if text:
                result["content"] = str(text).strip()[:MAX_CONTENT_CHARS]


def _decode(raw: Any) -> str:
    """Decode a raw HTTP body to text (bytes -> utf-8 with replacement)."""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def default_search_backend() -> WebSearchBackend:
    """Return a :class:`WebSearchBackend` configured from the runtime settings."""
    return WebSearchBackend()
