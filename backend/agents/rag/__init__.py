"""RAG façade for agent code (terv.md 7. fejezet, Research Agent).

The implementation lives in :mod:`embeddings.services` because the
``EmbeddingChunk`` model (and the pgvector column) belong to the ``embeddings``
app. This package only re-exports a stable ``agents.rag`` entry point so the
Research Agent never has to know where the storage lives, and so swap-outs
(local model, remote embedding service, ...) stay a one-file change.

Usage::

    from agents.rag import RagDisabledError, ingest_document, retrieve

    retrieve("M5 ISO 4762 dims", limit=5, company_id="acme")

Every entry point is guarded by the runtime ``rag_enabled`` setting (resolved
via ``configuration.services.get_setting``, default ``False``) and by
``settings.DB_IS_POSTGRES``; importing this package is always safe, even on the
sqlite fallback where the ``embeddings`` app is not installed.
"""

from __future__ import annotations

from embeddings.services import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OVERLAP,
    EmbeddingError,
    RagDisabledError,
    RagError,
    RagUnavailableError,
    chunk_text,
    delete_document,
    embed_text,
    ingest_document,
    rag_enabled,
    retrieve,
)

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_OVERLAP",
    "EmbeddingError",
    "RagDisabledError",
    "RagError",
    "RagUnavailableError",
    "chunk_text",
    "delete_document",
    "embed_text",
    "ingest_document",
    "rag_enabled",
    "retrieve",
]
