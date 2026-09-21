"""Slicing profiles (terv.md 12. fejezet).

The profiles are plain data; the slicing engine lives behind a
``SlicerBackend`` interface in ``slicers/services.py`` (slicer-worker's scope).
Concrete slicer CLIs (PrusaSlicer first, OrcaSlicer later) read these through
the service layer, never directly from a view.
"""

from django.db import models


class PrinterProfile(models.Model):
    """Slicer-side printer configuration (bed size, nozzle, kinematics...)."""

    name = models.CharField(max_length=200)
    printer_model = models.CharField(max_length=200, blank=True)
    backend = models.CharField(max_length=50, blank=True)
    settings_json = models.JSONField(default=dict, blank=True)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class FilamentProfile(models.Model):
    """Filament/material configuration used by the slicer."""

    name = models.CharField(max_length=200)
    material = models.CharField(max_length=100, blank=True)
    brand = models.CharField(max_length=100, blank=True)
    color = models.CharField(max_length=100, blank=True)
    settings_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class ProcessProfile(models.Model):
    """Print process settings (layer height + free-form slicer overrides)."""

    name = models.CharField(max_length=200)
    layer_height = models.FloatField(null=True, blank=True, help_text="Layer height in mm")
    settings_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name
