"""Embedding / RAG service for the PrintForge knowledge base.

Implements the infrastructure described in terv.md 3. fejezet (Embedding),
5. fejezet (``KnowledgeDocument`` / ``EmbeddingChunk``), 7. fejezet (Research
Agent) and 26. fejezet (testing).

Design constraints
------------------

* **Ollama only.** Embeddings are produced by Ollama over plain HTTP using the
  standard library (``urllib.request``) so the project gains no extra
  dependency and never requires a cloud embedding API.
* **sqlite-safe imports.** The pgvector-backed ``embeddings`` app is only
  registered in ``INSTALLED_APPS`` when the default database is PostgreSQL
  (``settings.DB_IS_POSTGRES``). The models are therefore imported lazily inside
  the functions, and every entry point is guarded, so
  ``import embeddings.services`` never breaks on the sqlite fallback.
* **Feature flag.** Everything is gated by ``settings.RAG_ENABLED`` (default
  ``False``). When it is off, :func:`retrieve` returns ``[]`` and
  :func:`ingest_document` raises :class:`RagDisabledError` without ever touching
  the database or a live embedding model.

The public API is re-exported by :mod:`agents.rag` for agent code.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

from django.conf import settings

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids sqlite import issues
    from embeddings.models import KnowledgeDocument

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_EMBEDDING_MODEL = "bge-m3"
DEFAULT_EMBEDDING_DIM = 1024

#: Ollama's batch embedding endpoint. It is called with a single string
#: ``input`` and answers with ``{"embeddings": [[...]]}``. The legacy
#: ``{"embedding": [...]}`` shape of ``/api/embeddings`` is also accepted.
EMBEDDINGS_PATH = "/api/embed"

EMBEDDING_TIMEOUT_SEC = 120.0

#: Target chunk size in characters. The content is often Hungarian, so a
#: character-based splitter is used instead of an English-centric tokenizer.
DEFAULT_CHUNK_SIZE = 1000
DEFAULT_OVERLAP = 200


class RagError(RuntimeError):
    """Base class for every RAG infrastructure error."""


class RagDisabledError(RagError):
    """Raised when a mutating RAG operation runs while ``RAG_ENABLED`` is off."""


class RagUnavailableError(RagError):
    """Raised when the pgvector ``embeddings`` app is not available (sqlite)."""


class EmbeddingError(RagError):
    """Raised when Ollama's embedding endpoint fails or answers unusably."""


# ---------------------------------------------------------------------------
# Feature-flag / availability guards
# ---------------------------------------------------------------------------


def rag_enabled() -> bool:
    """Return whether the RAG feature flag is on (default ``False``)."""
    return bool(getattr(settings, "RAG_ENABLED", False))


def postgres_available() -> bool:
    """Return whether the pgvector-backed ``embeddings`` app can be used."""
    return bool(getattr(settings, "DB_IS_POSTGRES", False))


def _setting(name: str, default: Any) -> Any:
    return getattr(settings, name, default)


def _models() -> tuple[type[KnowledgeDocument], type[Any]]:
    """Import the pgvector models lazily (never at module import time)."""
    from embeddings.models import EmbeddingChunk, KnowledgeDocument

    return KnowledgeDocument, EmbeddingChunk


# ---------------------------------------------------------------------------
# Embeddings (Ollama, stdlib only)
# ---------------------------------------------------------------------------


def embed_text(text: str) -> list[float]:
    """Embed *text* with the configured Ollama embedding model.

    Calls ``POST {OLLAMA_BASE_URL}/api/embed`` and returns the first vector.
    The function is intentionally a plain module-level callable so tests can
    ``monkeypatch`` it without a live model.
    """
    if not isinstance(text, str) or not text.strip():
        raise EmbeddingError("embed_text() requires a non-empty string")

    base_url = str(_setting("OLLAMA_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
    model = str(_setting("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL))
    url = f"{base_url}{EMBEDDINGS_PATH}"
    payload = {"model": model, "input": text}

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=EMBEDDING_TIMEOUT_SEC) as response:
            body = response.read().decode("utf-8")
    except TimeoutError as exc:
        raise EmbeddingError(
            f"Ollama embedding request timed out after {EMBEDDING_TIMEOUT_SEC}s: {url}"
        ) from exc
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise EmbeddingError(f"Ollama embedding HTTP {exc.code} on {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise EmbeddingError(f"Cannot reach Ollama for embeddings at {url}: {exc.reason}") from exc
    except OSError as exc:
        raise EmbeddingError(f"Ollama embedding request failed on {url}: {exc}") from exc

    try:
        decoded = json.loads(body)
    except json.JSONDecodeError as exc:
        raise EmbeddingError(f"Ollama returned invalid JSON on {url}: {body[:200]!r}") from exc

    vector = _extract_embedding(decoded)
    if vector is None:
        raise EmbeddingError(f"Ollama returned no embedding on {url}: {body[:200]!r}")
    _validate_dimension(vector)
    return vector


def _extract_embedding(payload: Any) -> list[float] | None:
    """Pull the first vector out of either Ollama response shape."""
    if not isinstance(payload, dict):
        return None

    embeddings = payload.get("embeddings")
    if isinstance(embeddings, list) and embeddings:
        first = embeddings[0]
        if isinstance(first, list) and first:
            return [float(value) for value in first]

    # Legacy /api/embeddings response.
    embedding = payload.get("embedding")
    if isinstance(embedding, list) and embedding:
        return [float(value) for value in embedding]
    return None


def _validate_dimension(vector: list[float]) -> None:
    expected = int(_setting("EMBEDDING_DIM", DEFAULT_EMBEDDING_DIM))
    if len(vector) != expected:
        raise EmbeddingError(
            f"Embedding has {len(vector)} dimensions but EMBEDDING_DIM={expected}. "
            "Change EMBEDDING_DIM or re-embed the knowledge base."
        )


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def chunk_text(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[str]:
    """Split *text* into overlapping, paragraph-aware chunks.

    Strategy: paragraphs are packed greedily up to ``chunk_size`` characters and
    any paragraph longer than ``chunk_size`` is hard-wrapped. When ``overlap``
    is greater than zero, the tail of the previous chunk is prepended to the
    next chunk (snapped to a word boundary) so retrieval keeps sentence
    context. Character-based splitting keeps it tokenizer-agnostic, which
    matters for the mostly Hungarian content.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0:
        raise ValueError("overlap must not be negative")
    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size")

    normalized = (text or "").strip()
    if not normalized:
        return []

    units: list[str] = []
    for paragraph in re.split(r"\n\s*\n", normalized):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= chunk_size:
            units.append(paragraph)
        else:
            units.extend(
                paragraph[i : i + chunk_size] for i in range(0, len(paragraph), chunk_size)
            )

    chunks: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}\n\n{unit}" if current else unit
        if len(candidate) <= chunk_size:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = unit
    if current:
        chunks.append(current)

    if overlap and len(chunks) > 1:
        overlapped = [chunks[0]]
        for previous, chunk in zip(chunks, chunks[1:], strict=False):
            tail = _overlap_tail(previous, overlap)
            overlapped.append(f"{tail}\n{chunk}" if tail else chunk)
        return overlapped
    return chunks


def _overlap_tail(previous: str, overlap: int) -> str:
    """Return a word-boundary snapped tail of *previous* (at most *overlap*)."""
    if overlap <= 0 or not previous:
        return ""
    tail = previous[-overlap:]
    for index, char in enumerate(tail):
        if char.isspace():
            return tail[index + 1 :]
    return tail


# ---------------------------------------------------------------------------
# Ingest / retrieve / delete
# ---------------------------------------------------------------------------


def ingest_document(
    *,
    source_type: str,
    title: str,
    content: str,
    source_url: str = "",
    company_id: str = "",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> KnowledgeDocument:
    """Chunk *content*, embed every chunk and store the document + chunks.

    Raises :class:`RagDisabledError` when ``RAG_ENABLED`` is off (before any
    model or database work) and :class:`RagUnavailableError` when the
    pgvector-backed app is not available (sqlite).
    """
    if not rag_enabled():
        raise RagDisabledError("RAG is disabled (RAG_ENABLED=false); ingest_document() is a no-op.")
    if not postgres_available():
        raise RagUnavailableError(
            "The embeddings app requires PostgreSQL + pgvector; "
            "set DATABASE_URL to a PostgreSQL instance."
        )

    KnowledgeDocument, EmbeddingChunk = _models()

    document = KnowledgeDocument.objects.create(
        source_type=source_type,
        title=title,
        content=content,
        source_url=source_url,
        company_id=company_id,
    )

    chunks = chunk_text(content, chunk_size=chunk_size, overlap=overlap)
    model_name = str(_setting("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL))
    EmbeddingChunk.objects.bulk_create(
        [
            EmbeddingChunk(
                document=document,
                chunk_index=index,
                content=chunk,
                embedding=embed_text(chunk),
                model=model_name,
            )
            for index, chunk in enumerate(chunks)
        ]
    )
    return document


def retrieve(
    query: str,
    limit: int = 8,
    company_id: str | None = None,
) -> list[dict]:
    """Return the closest chunks for *query* using pgvector cosine distance.

    Returns ``[{chunk_id, document_id, content, source_url, title, score}]``
    ordered by descending similarity (``score = 1 - cosine_distance``). The
    search is optionally scoped to a company/workspace via *company_id*.

    When ``RAG_ENABLED`` is off, or the pgvector app is not available, this is a
    cheap no-op returning ``[]`` -- no model call, no database query.
    """
    if not rag_enabled() or not postgres_available():
        return []
    if not isinstance(query, str) or not query.strip():
        return []
    if limit <= 0:
        return []

    from pgvector.django import CosineDistance

    _, EmbeddingChunk = _models()
    vector = embed_text(query)

    queryset = EmbeddingChunk.objects.select_related("document")
    if company_id:
        queryset = queryset.filter(document__company_id=company_id)
    queryset = queryset.annotate(distance=CosineDistance("embedding", vector)).order_by("distance")[
        :limit
    ]

    return [
        {
            "chunk_id": chunk.pk,
            "document_id": chunk.document_id,
            "content": chunk.content,
            "source_url": chunk.document.source_url,
            "title": chunk.document.title,
            "score": 1.0 - float(chunk.distance),
        }
        for chunk in queryset
    ]


def delete_document(document_id: int) -> int:
    """Delete a document and its chunks; returns the number of deleted rows.

    Safe no-op (returns ``0``) when the pgvector app is not available or RAG is
    disabled -- it never needs an embedding model.
    """
    if not postgres_available():
        return 0
    if not rag_enabled():
        return 0

    KnowledgeDocument, _ = _models()
    deleted, _details = KnowledgeDocument.objects.filter(pk=document_id).delete()
    return int(deleted)
