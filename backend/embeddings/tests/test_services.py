"""Tests for the embedding / RAG service (no live Ollama, sqlite-safe).

The transport tests monkeypatch ``urllib.request.urlopen``; the round-trip test
stubs ``embed_text`` entirely. Nothing here needs network access.
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pytest
from django.conf import settings

from embeddings import services
from embeddings.services import (
    EmbeddingError,
    RagDisabledError,
    chunk_text,
    embed_text,
    ingest_document,
    retrieve,
)

# ---------------------------------------------------------------------------
# Import & feature-flag gating (runs on sqlite)
# ---------------------------------------------------------------------------


@pytest.fixture
def runtime_settings(monkeypatch):
    """Patch the runtime-settings resolver with an in-memory mapping.

    The RAG flag and the embedding model now come from
    ``configuration.services.get_setting`` (runtime-editable, DB override ->
    Django settings -> env -> default), so tests patch that service rather than
    mutating Django settings.
    """
    values: dict[str, Any] = {"rag_enabled": False, "embedding_model": "bge-m3"}

    def fake_get_setting(name: str) -> Any:
        return values.get(name)

    monkeypatch.setattr(services, "get_setting", fake_get_setting)
    return values


def test_module_imports_without_touching_models():
    """Importing on sqlite must not require the pgvector app to be installed."""
    assert callable(services.embed_text)
    assert callable(services.retrieve)
    assert services.DEFAULT_CHUNK_SIZE > 0


def test_rag_enabled_reads_the_runtime_service(runtime_settings):
    runtime_settings["rag_enabled"] = True
    assert services.rag_enabled() is True
    runtime_settings["rag_enabled"] = False
    assert services.rag_enabled() is False


def test_retrieve_returns_empty_when_rag_disabled(runtime_settings):
    runtime_settings["rag_enabled"] = False
    assert retrieve("M5 csavar") == []


def test_ingest_raises_when_rag_disabled(runtime_settings):
    runtime_settings["rag_enabled"] = False
    with pytest.raises(RagDisabledError):
        ingest_document(source_type="manual", title="x", content="hello")


def test_ingest_raises_unavailable_on_non_postgres(runtime_settings, settings):
    if settings.DB_IS_POSTGRES:
        pytest.skip("sqlite-specific: the pgvector app is installed on PostgreSQL")
    runtime_settings["rag_enabled"] = True
    with pytest.raises(services.RagUnavailableError):
        ingest_document(source_type="manual", title="x", content="hello")


def test_delete_is_safe_noop_when_rag_disabled(runtime_settings):
    runtime_settings["rag_enabled"] = False
    assert services.delete_document(1) == 0


# ---------------------------------------------------------------------------
# embed_text transport (stubbed HTTP)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _stub_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    captured: list[Any] | None = None,
) -> None:
    def fake_urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
        if captured is not None:
            captured.append((request, timeout))
        return _FakeResponse(payload)

    monkeypatch.setattr(services.urllib.request, "urlopen", fake_urlopen)


def test_embed_text_posts_to_ollama_api_embed(monkeypatch, settings, runtime_settings):
    settings.OLLAMA_BASE_URL = "http://test:11434/"
    settings.EMBEDDING_DIM = 3
    runtime_settings["embedding_model"] = "bge-m3"
    captured: list[Any] = []
    _stub_urlopen(monkeypatch, {"embeddings": [[0.1, 0.2, 0.3]]}, captured)

    assert embed_text("hello") == [0.1, 0.2, 0.3]

    request, timeout = captured[0]
    assert request.full_url == "http://test:11434/api/embed"
    assert json.loads(request.data.decode("utf-8")) == {"model": "bge-m3", "input": "hello"}
    assert timeout == services.EMBEDDING_TIMEOUT_SEC


def test_embed_text_accepts_legacy_embedding_response(monkeypatch, settings, runtime_settings):
    settings.EMBEDDING_DIM = 2
    _stub_urlopen(monkeypatch, {"embedding": [1.0, 2.0]})
    assert embed_text("x") == [1.0, 2.0]


def test_embed_text_rejects_empty_input():
    with pytest.raises(EmbeddingError):
        embed_text("   ")


def test_embed_text_rejects_missing_vector(monkeypatch, runtime_settings):
    _stub_urlopen(monkeypatch, {"done": True})
    with pytest.raises(EmbeddingError, match="no embedding"):
        embed_text("hi")


def test_embed_text_rejects_dimension_mismatch(monkeypatch, settings, runtime_settings):
    settings.EMBEDDING_DIM = 1024
    _stub_urlopen(monkeypatch, {"embeddings": [[0.1, 0.2]]})
    with pytest.raises(EmbeddingError, match="EMBEDDING_DIM"):
        embed_text("hi")


def test_embed_text_wraps_connection_error(monkeypatch, runtime_settings):
    def boom(request: Any, timeout: float | None = None) -> None:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(services.urllib.request, "urlopen", boom)
    with pytest.raises(EmbeddingError, match="Cannot reach Ollama"):
        embed_text("hi")


def test_embed_text_wraps_http_error(monkeypatch, runtime_settings):
    def boom(request: Any, timeout: float | None = None) -> None:
        raise urllib.error.HTTPError(
            "http://test/api/embed", 500, "boom", {}, io.BytesIO(b"server error")
        )

    monkeypatch.setattr(services.urllib.request, "urlopen", boom)
    with pytest.raises(EmbeddingError, match="HTTP 500"):
        embed_text("hi")


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def test_chunk_text_empty_returns_no_chunks():
    assert chunk_text("   ") == []


def test_chunk_text_short_text_is_one_chunk():
    assert chunk_text("hello world") == ["hello world"]


def test_chunk_text_splits_on_paragraphs():
    text = "a" * 50 + "\n\n" + "b" * 50
    assert chunk_text(text, chunk_size=60, overlap=0) == ["a" * 50, "b" * 50]


def test_chunk_text_hard_wraps_long_paragraph():
    chunks = chunk_text("x" * 250, chunk_size=100, overlap=0)
    assert len(chunks) == 3
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "".join(chunks) == "x" * 250


def test_chunk_text_applies_overlap():
    text = "a" * 50 + "\n\n" + "b" * 50
    chunks = chunk_text(text, chunk_size=60, overlap=10)
    assert len(chunks) == 2
    assert chunks[1].startswith("a" * 10)


def test_chunk_text_rejects_invalid_parameters():
    with pytest.raises(ValueError):
        chunk_text("hi", chunk_size=10, overlap=10)
    with pytest.raises(ValueError):
        chunk_text("hi", chunk_size=0)
    with pytest.raises(ValueError):
        chunk_text("hi", chunk_size=10, overlap=-1)


# ---------------------------------------------------------------------------
# Round-trip (PostgreSQL + pgvector only)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not settings.DB_IS_POSTGRES,
    reason="embeddings needs PostgreSQL + pgvector (skipped on sqlite)",
)
@pytest.mark.django_db
def test_ingest_then_retrieve_round_trip(monkeypatch, settings, runtime_settings):
    runtime_settings["rag_enabled"] = True
    dimension = settings.EMBEDDING_DIM
    calls: list[str] = []

    def fake_embed(text: str) -> list[float]:
        calls.append(text)
        # Deterministic vector: identical for query and chunks, so the cosine
        # similarity of the stored chunk to its own query is exactly 1.0.
        return [1.0 + (index % 3) for index in range(dimension)]

    monkeypatch.setattr(services, "embed_text", fake_embed)

    document = ingest_document(
        source_type="standard",
        title="ISO 4762",
        content="M5 socket head cap screw dimensions. " * 20,
        source_url="https://example.test/iso4762",
        company_id="acme",
        chunk_size=120,
        overlap=20,
    )

    assert document.pk is not None
    assert document.chunks.count() >= 1
    assert calls, "ingest must embed every chunk"

    results = retrieve("M5 screw dimensions", limit=5, company_id="acme")

    assert results
    top = results[0]
    assert set(top) == {"chunk_id", "document_id", "content", "source_url", "title", "score"}
    assert top["document_id"] == document.pk
    assert top["title"] == "ISO 4762"
    assert top["source_url"] == "https://example.test/iso4762"
    assert -1.0 <= top["score"] <= 1.0
    assert top["score"] == pytest.approx(1.0, abs=1e-6)

    # Company scoping must exclude the document.
    assert retrieve("M5 screw", limit=5, company_id="other") == []

    assert services.delete_document(document.pk) >= 1
    assert not services.delete_document(document.pk)
