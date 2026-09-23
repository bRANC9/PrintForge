"""Web-search backend tests (agents/search.py, terv.md 7. fejezet).

No network is ever touched: the HTTP seam (``http_get``), the HTML extractor and
the page-safety check are all injected. The tests pin the SearXNG JSON
contract, the best-effort failure modes, the bounded payloads and the SSRF
guard for page fetching.
"""

from __future__ import annotations

import json
from typing import Any

from agents import search as search_module
from agents.search import (
    MAX_CONTENT_CHARS,
    SEARXNG,
    WebSearchBackend,
    is_public_http_url,
)

SEARXNG_PAYLOAD = {
    "query": "Galaxy S24 dimensions",
    "number_of_results": 2,
    "results": [
        {
            "title": "Samsung Galaxy S24",
            "url": "https://example.test/s24",
            "content": "70.6 mm x 147.0 mm x 7.6 mm",
            "engine": "duckduckgo",
        },
        {
            "title": "GSMArena",
            "url": "https://example.test/gsmarena",
            "snippet": "body dimensions 147.0 x 70.6 x 7.6 mm",
        },
        {"title": "no url", "content": "dropped"},
    ],
}


def _backend(*, settings: dict[str, str], responses: dict[str, Any], **kwargs: Any):
    """Build a backend whose settings and HTTP responses are fully injected."""
    http = _Recorder(responses)
    backend = WebSearchBackend(
        base_url=settings.get("searxng_base_url", ""),
        http_get=http,
        extract=kwargs.pop("extract", lambda html: f"text:{html}"),
        **kwargs,
    )
    return backend, http


class _Recorder:
    """Records requested URLs and returns canned bodies (or raises)."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def __call__(self, url: str, *, timeout: float = 0.0) -> bytes:
        self.urls.append(url)
        if url in self.responses:
            value = self.responses[url]
            if isinstance(value, Exception):
                raise value
            return value.encode("utf-8") if isinstance(value, str) else value
        for prefix, value in self.responses.items():
            if prefix in url:
                if isinstance(value, Exception):
                    raise value
                return value.encode("utf-8") if isinstance(value, str) else value
        raise AssertionError(f"unexpected URL {url}")


def _patch_settings(monkeypatch, settings: dict[str, str]) -> None:
    """Make the backend resolve settings from a plain dict (no DB).

    ``WebSearchBackend.settings`` is replaced so the injected ``base_url`` wins;
    ``_get_setting`` is patched too so the *enabled* gate reads the same dict.
    """

    def fake_settings(self: WebSearchBackend) -> tuple[str, str]:
        base = self._base_url_override or settings.get("searxng_base_url", "")
        return settings.get("search_backend", "").strip().lower(), str(base).rstrip("/")

    monkeypatch.setattr(WebSearchBackend, "settings", fake_settings)
    monkeypatch.setattr(
        search_module,
        "_get_setting",
        lambda name, default="": settings.get(name, default),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_search_returns_title_url_snippet_and_skips_urlless(monkeypatch):
    settings = {"search_backend": SEARXNG, "searxng_base_url": "http://searxng:8080"}
    responses = {"http://searxng:8080/search": json.dumps(SEARXNG_PAYLOAD)}
    backend, http = _backend(settings=settings, responses=responses)
    _patch_settings(monkeypatch, settings)
    monkeypatch.setattr(search_module, "is_public_http_url", lambda url: False)

    results = backend.search("Galaxy S24 dimensions")

    assert [item["title"] for item in results] == ["Samsung Galaxy S24", "GSMArena"]
    assert results[0]["url"] == "https://example.test/s24"
    assert results[0]["snippet"] == "70.6 mm x 147.0 mm x 7.6 mm"
    # The second result only had a ``snippet`` key; it is normalised into one.
    assert results[1]["snippet"] == "body dimensions 147.0 x 70.6 x 7.6 mm"
    assert "content" not in results[0]  # page fetching was disabled by the guard
    # The request carried the JSON format + the query.
    assert http.urls == [
        "http://searxng:8080/search?q=Galaxy+S24+dimensions&format=json&safesearch=1&language=all"
    ]


def test_search_fetches_and_extracts_top_results(monkeypatch):
    settings = {"search_backend": SEARXNG, "searxng_base_url": "http://searxng:8080"}
    responses = {
        "http://searxng:8080/search": json.dumps(SEARXNG_PAYLOAD),
        "https://example.test/s24": "<html>main text</html>",
        "https://example.test/gsmarena": "<html>other text</html>",
    }
    backend, http = _backend(settings=settings, responses=responses, fetch_results=1)
    _patch_settings(monkeypatch, settings)
    monkeypatch.setattr(search_module, "is_public_http_url", lambda url: True)

    results = backend.search("q", limit=2)

    assert results[0]["content"] == "text:<html>main text</html>"
    # Only the top ``fetch_results`` URL was fetched (bounded work).
    assert results[1].get("content") is None
    assert http.urls[-1] == "https://example.test/s24"


def test_search_bounds_the_result_count(monkeypatch):
    settings = {"search_backend": SEARXNG, "searxng_base_url": "http://searxng:8080"}
    payload = {"results": [dict(SEARXNG_PAYLOAD["results"][0])] * 10}
    backend, _http = _backend(
        settings=settings, responses={"http://searxng:8080/search": json.dumps(payload)}
    )
    _patch_settings(monkeypatch, settings)

    assert len(backend.search("q", limit=100)) == 5  # DEFAULT_MAX_RESULTS


def test_extracted_content_is_bounded(monkeypatch):
    settings = {"search_backend": SEARXNG, "searxng_base_url": "http://searxng:8080"}
    backend, _http = _backend(
        settings=settings,
        responses={
            "http://searxng:8080/search": json.dumps(SEARXNG_PAYLOAD),
            "https://example.test/s24": "<html/>",
        },
        extract=lambda html: "x" * (MAX_CONTENT_CHARS + 500),
        fetch_results=1,
    )
    _patch_settings(monkeypatch, settings)
    monkeypatch.setattr(search_module, "is_public_http_url", lambda url: True)

    results = backend.search("q")

    assert len(results[0]["content"]) == MAX_CONTENT_CHARS


# ---------------------------------------------------------------------------
# Best-effort failure modes
# ---------------------------------------------------------------------------


def test_search_is_a_noop_when_disabled(monkeypatch):
    settings = {"search_backend": "", "searxng_base_url": "http://searxng:8080"}
    backend, http = _backend(settings=settings, responses={})
    _patch_settings(monkeypatch, settings)

    assert backend.enabled is False
    assert backend.search("q") == []
    assert http.urls == []


def test_search_is_a_noop_without_a_base_url(monkeypatch):
    settings = {"search_backend": SEARXNG, "searxng_base_url": ""}
    backend, http = _backend(settings=settings, responses={})
    _patch_settings(monkeypatch, settings)

    assert backend.enabled is False
    assert backend.search("q") == []
    assert http.urls == []


def test_empty_query_short_circuits(monkeypatch):
    settings = {"search_backend": SEARXNG, "searxng_base_url": "http://searxng:8080"}
    backend, http = _backend(settings=settings, responses={})
    _patch_settings(monkeypatch, settings)

    assert backend.search("   ") == []
    assert http.urls == []


def test_transport_error_returns_empty_and_never_raises(monkeypatch):
    settings = {"search_backend": SEARXNG, "searxng_base_url": "http://searxng:8080"}
    backend, _http = _backend(
        settings=settings,
        responses={"http://searxng:8080/search": OSError("connection refused")},
    )
    _patch_settings(monkeypatch, settings)

    assert backend.search("q") == []


def test_invalid_json_returns_empty(monkeypatch):
    settings = {"search_backend": SEARXNG, "searxng_base_url": "http://searxng:8080"}
    backend, _http = _backend(
        settings=settings, responses={"http://searxng:8080/search": "not-json"}
    )
    _patch_settings(monkeypatch, settings)

    assert backend.search("q") == []


def test_unexpected_payload_shape_returns_empty(monkeypatch):
    settings = {"search_backend": SEARXNG, "searxng_base_url": "http://searxng:8080"}
    backend, _http = _backend(
        settings=settings, responses={"http://searxng:8080/search": json.dumps({"results": None})}
    )
    _patch_settings(monkeypatch, settings)

    assert backend.search("q") == []


def test_page_fetch_failure_is_ignored(monkeypatch):
    settings = {"search_backend": SEARXNG, "searxng_base_url": "http://searxng:8080"}
    backend, _http = _backend(
        settings=settings,
        responses={
            "http://searxng:8080/search": json.dumps(SEARXNG_PAYLOAD),
            "https://example.test/s24": RuntimeError("timeout"),
        },
        fetch_results=1,
    )
    _patch_settings(monkeypatch, settings)
    monkeypatch.setattr(search_module, "is_public_http_url", lambda url: True)

    results = backend.search("q")

    # The result survives without its ``content`` field.
    assert results[0]["title"] == "Samsung Galaxy S24"
    assert "content" not in results[0]


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------


def test_is_public_http_url_blocks_non_http_and_local(monkeypatch):
    assert is_public_http_url("ftp://example.com/x") is False
    assert is_public_http_url("file:///etc/passwd") is False
    assert is_public_http_url("") is False
    assert is_public_http_url("http://localhost/x") is False
    assert is_public_http_url("http://printer.local/x") is False


def test_is_public_http_url_blocks_private_addresses(monkeypatch):
    monkeypatch.setattr(
        search_module.socket,
        "getaddrinfo",
        lambda host, port: [(None, None, None, None, ("127.0.0.1", 0))],
    )
    assert is_public_http_url("http://searxng.internal/x") is False

    monkeypatch.setattr(
        search_module.socket,
        "getaddrinfo",
        lambda host, port: [(None, None, None, None, ("192.168.1.10", 0))],
    )
    assert is_public_http_url("http://internal/x") is False


def test_is_public_http_url_allows_a_public_address(monkeypatch):
    monkeypatch.setattr(
        search_module.socket,
        "getaddrinfo",
        lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))],
    )
    assert is_public_http_url("https://example.com/x") is True


def test_is_public_http_url_blocks_unresolvable_hosts(monkeypatch):
    def boom(host, port):
        raise OSError("name resolution failed")

    monkeypatch.setattr(search_module.socket, "getaddrinfo", boom)

    assert is_public_http_url("https://nowhere.invalid/x") is False
