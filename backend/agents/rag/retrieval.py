"""Vector retrieval entry point (pgvector cosine distance / HNSW).

Re-exports :func:`embeddings.services.retrieve` for the Research Agent.
"""

from __future__ import annotations

from embeddings.services import retrieve

__all__ = ["retrieve"]
