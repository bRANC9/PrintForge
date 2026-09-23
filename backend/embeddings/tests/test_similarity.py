"""Tests for the best-effort text-similarity helper (no live model).

Covers the ranking order with an injected fake embedder and the graceful
degradation paths (RAG disabled, unreachable embedder, empty inputs) that must
return ``[]`` without raising. Nothing here needs network access.
"""

from __future__ import annotations

import urllib.error
from typing import Any

import pytest

from embeddings import services
from embeddings.services import EmbeddingError, cosine_similarity, rank_texts


@pytest.fixture
def runtime_settings(monkeypatch):
    """Patch the runtime-settings resolver with an in-memory mapping.

    Mirrors the fixture in ``test_services.py``: the RAG flag is read through
    ``configuration.services.get_setting`` at call time.
    """
    values: dict[str, Any] = {"rag_enabled": True, "embedding_model": "bge-m3"}

    def fake_get_setting(name: str) -> Any:
        return values.get(name)

    monkeypatch.setattr(services, "get_setting", fake_get_setting)
    return values


#: Query is ``[1, 0]``; "alpha" is identical, "gamma" is 0.6, "beta" orthogonal.
_VECTORS = {
    "query": [1.0, 0.0],
    "alpha": [1.0, 0.0],
    "beta": [0.0, 1.0],
    "gamma": [0.6, 0.8],
}


def _fake_embedder(calls: list[str] | None = None):
    def embed(text: str) -> list[float]:
        if calls is not None:
            calls.append(text)
        return list(_VECTORS[text])

    return embed


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def test_rank_texts_orders_by_descending_cosine_similarity(runtime_settings):
    ranked = rank_texts("query", ["alpha", "beta", "gamma"], embedder=_fake_embedder())

    assert [index for index, _ in ranked] == [0, 2, 1]
    assert ranked[0][1] == pytest.approx(1.0)
    assert ranked[1][1] == pytest.approx(0.6)
    assert ranked[2][1] == pytest.approx(0.0)


def test_rank_texts_respects_limit(runtime_settings):
    ranked = rank_texts("query", ["alpha", "beta", "gamma"], limit=2, embedder=_fake_embedder())
    assert [index for index, _ in ranked] == [0, 2]


def test_rank_texts_skips_blank_candidates_but_keeps_indices(runtime_settings):
    ranked = rank_texts("query", ["", "alpha", "   "], embedder=_fake_embedder())

    assert len(ranked) == 1
    assert ranked[0][0] == 1
    assert ranked[0][1] == pytest.approx(1.0)


def test_rank_texts_skips_unembeddable_candidate(runtime_settings):
    def embed(text: str) -> list[float]:
        if text == "beta":
            raise EmbeddingError("boom")
        return list(_VECTORS[text])

    ranked = rank_texts("query", ["alpha", "beta", "gamma"], embedder=embed)
    assert [index for index, _ in ranked] == [0, 2]


def test_rank_texts_uses_module_embed_text_by_default(monkeypatch, runtime_settings):
    seen: list[str] = []

    def fake_embed(text: str) -> list[float]:
        seen.append(text)
        return list(_VECTORS[text])

    monkeypatch.setattr(services, "embed_text", fake_embed)
    ranked = rank_texts("query", ["alpha", "beta"])

    assert seen == ["query", "alpha", "beta"]
    assert [index for index, _ in ranked] == [0, 1]


# ---------------------------------------------------------------------------
# Graceful degradation (never raise)
# ---------------------------------------------------------------------------


def test_rank_texts_returns_empty_when_rag_disabled(runtime_settings):
    runtime_settings["rag_enabled"] = False
    calls: list[str] = []

    assert rank_texts("query", ["alpha"], embedder=_fake_embedder(calls)) == []
    assert calls == [], "the embedder must not be called when RAG is disabled"


@pytest.mark.parametrize(
    "error",
    [
        EmbeddingError("Cannot reach Ollama"),
        urllib.error.URLError("connection refused"),
        ConnectionError("network down"),
        TimeoutError("timed out"),
    ],
)
def test_rank_texts_returns_empty_when_embedder_unreachable(runtime_settings, error):
    def boom(text: str) -> list[float]:
        raise error

    assert rank_texts("query", ["alpha", "beta"], embedder=boom) == []


@pytest.mark.parametrize(
    ("query", "texts"),
    [
        ("", ["alpha"]),
        ("   ", ["alpha"]),
        ("query", []),
    ],
)
def test_rank_texts_empty_inputs_return_empty(runtime_settings, query, texts):
    assert rank_texts(query, texts, embedder=_fake_embedder()) == []


def test_rank_texts_non_positive_limit_returns_empty(runtime_settings):
    assert rank_texts("query", ["alpha"], limit=0, embedder=_fake_embedder()) == []


# ---------------------------------------------------------------------------
# cosine_similarity edge cases
# ---------------------------------------------------------------------------


def test_cosine_similarity_identical_is_one():
    assert cosine_similarity([1.0, 2.0], [1.0, 2.0]) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_is_zero():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_opposite_is_negative_one():
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ([], [1.0]),
        ([1.0], []),
        ([1.0, 2.0], [1.0]),
        ([0.0, 0.0], [1.0, 0.0]),
        ([1.0, 0.0], [0.0, 0.0]),
    ],
)
def test_cosine_similarity_unusable_inputs_are_zero(left, right):
    assert cosine_similarity(left, right) == 0.0
