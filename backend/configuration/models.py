"""Runtime-editable application settings and Ollama pull tracking.

:class:`AppSettings` is a singleton (``pk=1``) of optional overrides: every
field is optional (``blank``/``null``) and an empty value means "fall back to
the environment variable, then to the code default". The resolution order
lives in ``configuration/services.py`` (api-dev's scope); this module only
stores the overrides and guarantees there is exactly one row.

:class:`OllamaPull` persists the progress of a long-running model pull so the
Celery task and the UI can read the same state.

``EMBEDDING_DIM`` is intentionally *not* a field: it is baked into the pgvector
column and its migration, so changing it at runtime would corrupt the schema.
"""

from django.conf import settings
from django.db import models

MODE_CHOICES = [("local", "local"), ("docker", "docker")]
STORAGE_BACKEND_CHOICES = [("local", "local"), ("s3", "s3")]


class OllamaPullStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    RUNNING = "RUNNING", "Running"
    DONE = "DONE", "Done"
    FAILED = "FAILED", "Failed"


class AppSettings(models.Model):
    """Singleton row (pk=1) holding admin-editable overrides.

    Use :meth:`load` to fetch the singleton; it is created on first access.
    """

    ollama_base_url = models.URLField(blank=True)
    ollama_model = models.CharField(max_length=200, blank=True)
    ollama_vision_model = models.CharField(max_length=200, blank=True)
    # LLM provider selection: "ollama" (default) or an OpenAI-compatible
    # endpoint. ``openai_api_key`` is a credential; this model has no dedicated
    # secret field, so it is stored as a plain CharField.
    llm_provider = models.CharField(max_length=50, blank=True)
    openai_base_url = models.URLField(blank=True)
    openai_api_key = models.CharField(max_length=255, blank=True)
    embedding_model = models.CharField(max_length=200, blank=True)
    rag_enabled = models.BooleanField(null=True, blank=True)
    # Web search for the Research agent; empty disables it.
    search_backend = models.CharField(max_length=50, blank=True)
    searxng_base_url = models.URLField(blank=True)
    openscad_mode = models.CharField(max_length=20, blank=True, choices=MODE_CHOICES)
    openscad_timeout_sec = models.PositiveIntegerField(null=True, blank=True)
    openscad_memory_limit = models.CharField(max_length=20, blank=True)
    openscad_cpu_limit = models.CharField(max_length=20, blank=True)
    slicer_mode = models.CharField(max_length=20, blank=True, choices=MODE_CHOICES)
    slicer_timeout_sec = models.PositiveIntegerField(null=True, blank=True)
    storage_backend = models.CharField(
        max_length=20,
        blank=True,
        choices=STORAGE_BACKEND_CHOICES,
    )
    # MCP transport identity: the numeric user id as a string (empty = unset).
    mcp_service_user_id = models.CharField(max_length=20, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "app settings"
        verbose_name_plural = "app settings"

    def __str__(self) -> str:
        return "App settings"

    def save(self, *args, **kwargs):
        # Force the singleton primary key: a second row can never be created,
        # even through a race or a direct admin/ORM save.
        self.pk = 1
        return super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "AppSettings":
        """Return the singleton, creating it on first access."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class OllamaPull(models.Model):
    """Progress of an Ollama model pull, persisted across the Celery task.

    Pulling a model takes minutes, so the task (api-dev's scope) creates a row,
    moves it through :class:`OllamaPullStatus`, stores byte/percent progress,
    and finishes with ``DONE`` or ``FAILED`` (+ ``error``). This model only
    stores state.
    """

    name = models.CharField(max_length=200)
    status = models.CharField(
        max_length=20,
        choices=OllamaPullStatus.choices,
        default=OllamaPullStatus.PENDING,
    )
    progress_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    detail = models.CharField(max_length=300, blank=True)
    completed_bytes = models.BigIntegerField(null=True, blank=True)
    total_bytes = models.BigIntegerField(null=True, blank=True)
    error = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ollama_pulls",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "ollama pull"

    def __str__(self) -> str:
        return f"{self.name} ({self.status})"
