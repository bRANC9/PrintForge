"""Runtime-editable application settings (singleton ``pk=1``).

Every field is optional (``blank``/``null``): an empty value means "fall back
to the environment variable, then to the code default". The resolution order
lives in ``configuration/services.py`` (api-dev's scope); this module only
stores the overrides and guarantees there is exactly one row.

``EMBEDDING_DIM`` is intentionally *not* a field: it is baked into the pgvector
column and its migration, so changing it at runtime would corrupt the schema.
"""

from django.conf import settings
from django.db import models

MODE_CHOICES = [("local", "local"), ("docker", "docker")]
STORAGE_BACKEND_CHOICES = [("local", "local")]


class AppSettings(models.Model):
    """Singleton row (pk=1) holding admin-editable overrides.

    Use :meth:`load` to fetch the singleton; it is created on first access.
    """

    ollama_base_url = models.URLField(blank=True)
    ollama_model = models.CharField(max_length=200, blank=True)
    embedding_model = models.CharField(max_length=200, blank=True)
    rag_enabled = models.BooleanField(null=True, blank=True)
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
