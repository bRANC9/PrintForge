"""Embedding primitives for the RAG layer.

Re-exports the Ollama-backed embedding call and the chunker from
:mod:`embeddings.services`. Kept as a separate module so future embedding
backends (e.g. a local ``sentence-transformers`` model) can be added here
without touching retrieval or ingest.
"""

from __future__ import annotations

from embeddings.services import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OVERLAP,
    EmbeddingError,
    chunk_text,
    embed_text,
)

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_OVERLAP",
    "EmbeddingError",
    "chunk_text",
    "embed_text",
]
