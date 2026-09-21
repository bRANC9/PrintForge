"""Knowledge-base ingest entry points.

Re-exports :func:`embeddings.services.ingest_document` (chunk + embed + store)
and :func:`embeddings.services.delete_document`.
"""

from __future__ import annotations

from embeddings.services import (
    RagDisabledError,
    RagUnavailableError,
    delete_document,
    ingest_document,
)

__all__ = [
    "RagDisabledError",
    "RagUnavailableError",
    "delete_document",
    "ingest_document",
]
