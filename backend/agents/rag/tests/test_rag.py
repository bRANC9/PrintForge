"""Tests for the ``agents.rag`` façade (sqlite-safe, no live model)."""

from __future__ import annotations

import pytest

import agents.rag as rag
from embeddings import services
from embeddings.services import RagDisabledError


def _disable_rag(monkeypatch):
    """Patch the runtime-settings resolver used by the RAG service."""
    monkeypatch.setattr(services, "get_setting", lambda name: False)


def test_facade_exposes_the_public_api():
    for name in (
        "embed_text",
        "ingest_document",
        "retrieve",
        "delete_document",
        "chunk_text",
        "rag_enabled",
    ):
        assert hasattr(rag, name), name


def test_facade_retrieve_is_empty_when_disabled(monkeypatch):
    _disable_rag(monkeypatch)
    assert rag.retrieve("bármi") == []


def test_facade_ingest_raises_when_disabled(monkeypatch):
    _disable_rag(monkeypatch)
    with pytest.raises(RagDisabledError):
        rag.ingest_document(source_type="web", title="t", content="c")


def test_submodules_reexport_the_same_callables():
    from agents.rag import embeddings, ingest, retrieval

    assert embeddings.embed_text is rag.embed_text
    assert embeddings.chunk_text is rag.chunk_text
    assert ingest.ingest_document is rag.ingest_document
    assert retrieval.retrieve is rag.retrieve
