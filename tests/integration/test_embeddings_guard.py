"""Guard for the PostgreSQL-only ``embeddings`` app (Wave A).

``embeddings`` uses ``pgvector`` (``VectorField`` + HNSW index) and is only
registered in ``INSTALLED_APPS`` when the default database is PostgreSQL. On the
sqlite fallback (local runs / this suite) the app must be absent so nothing
accidentally queries it. On Postgres the models must be importable.
"""

from __future__ import annotations

import pytest
from django.conf import settings


def test_embeddings_app_is_registered_only_on_postgres():
    if settings.DB_IS_POSTGRES:
        assert "embeddings" in settings.INSTALLED_APPS
    else:
        assert "embeddings" not in settings.INSTALLED_APPS


@pytest.mark.skipif(
    not settings.DB_IS_POSTGRES,
    reason="embeddings needs PostgreSQL + pgvector (skipped on sqlite)",
)
def test_embeddings_models_import_on_postgres():
    from embeddings.models import EmbeddingChunk, KnowledgeDocument

    assert KnowledgeDocument._meta.app_label == "embeddings"
    assert EmbeddingChunk._meta.app_label == "embeddings"
