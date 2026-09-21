"""pgvector-backed knowledge base for RAG (terv.md 5. fejezet).

.. important::

   This app is **only** registered in ``INSTALLED_APPS`` when the default
   database is PostgreSQL (see ``DB_IS_POSTGRES`` in ``config.settings``).
   ``pgvector.django.VectorField`` and the ``vector`` extension cannot run on
   sqlite, so on the sqlite fallback (used by ``uv run pytest`` and docker-less
   local development) the whole app is left out and these models do not exist.
   Feature code (``backend/agents/rag/**``) must therefore guard any import or
   query behind ``settings.DB_IS_POSTGRES`` / ``settings.RAG_ENABLED``.

The embedding dimension comes from ``settings.EMBEDDING_DIM`` (env
``EMBEDDING_DIM``, default 1024 for ``bge-m3``). Changing the embedding model
means re-embedding every chunk; the column dimension is fixed by the schema.
"""

from django.conf import settings
from django.db import models
from pgvector.django import HnswIndex, VectorField


class KnowledgeSourceType(models.TextChoices):
    STANDARD = "standard", "Standard"
    DATASHEET = "datasheet", "Datasheet"
    WEB = "web", "Web"
    MANUAL = "manual", "Manual"


class KnowledgeDocument(models.Model):
    """A raw piece of knowledge (standard, datasheet, web page, manual)."""

    source_type = models.CharField(max_length=20, choices=KnowledgeSourceType.choices)
    source_url = models.URLField(blank=True)
    title = models.CharField(max_length=500, blank=True)
    content = models.TextField(blank=True)
    company_id = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title or f"KnowledgeDocument #{self.pk}"


class EmbeddingChunk(models.Model):
    """One embedded chunk of a :class:`KnowledgeDocument`.

    Similarity search uses the HNSW index on ``embedding`` with cosine
    distance.
    """

    document = models.ForeignKey(
        KnowledgeDocument,
        on_delete=models.CASCADE,
        related_name="chunks",
    )
    chunk_index = models.PositiveIntegerField()
    content = models.TextField()
    embedding = VectorField(dimensions=settings.EMBEDDING_DIM)
    model = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["document", "chunk_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "chunk_index"],
                name="uniq_embedding_chunk_index",
            )
        ]
        indexes = [
            HnswIndex(
                name="embeddingchunk_hnsw_cosine",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            )
        ]

    def __str__(self) -> str:
        return f"{self.document} #{self.chunk_index}"
