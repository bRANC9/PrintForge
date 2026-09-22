"""Slicing profiles (terv.md 12. fejezet).

The profiles are plain data; the slicing engine lives behind a
``SlicerBackend`` interface in ``slicers/services.py`` (slicer-worker's scope).
Concrete slicer CLIs (PrusaSlicer first, OrcaSlicer later) read these through
the service layer, never directly from a view.
"""

from django.conf import settings
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


class BuildPlate(models.Model):
    """Egy tálca-elrendezés több modellel (terv.md 28. fejezet).

    A tálca a projekthez tartozik, és opcionálisan egy printer-profilt köt.
    Az elrendezést a ``PlateItem`` sorok írják le.
    """

    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        related_name="build_plates",
    )
    name = models.CharField(max_length=200)
    printer_profile = models.ForeignKey(
        PrinterProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="build_plates",
    )
    settings_json = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_build_plates",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "name"],
                name="uniq_build_plate_name_per_project",
            )
        ]

    def __str__(self) -> str:
        return f"{self.project} - {self.name}"


class PlateItem(models.Model):
    """Egy modell a tálcán pozícióval, rotációval és skálával."""

    build_plate = models.ForeignKey(
        BuildPlate,
        on_delete=models.CASCADE,
        related_name="items",
    )
    model_version = models.ForeignKey(
        "designs.ModelVersion",
        on_delete=models.CASCADE,
        related_name="plate_items",
    )
    position_x = models.FloatField(default=0.0)
    position_y = models.FloatField(default=0.0)
    position_z = models.FloatField(default=0.0)
    rotation_z = models.FloatField(default=0.0)
    scale = models.FloatField(default=1.0)
    settings_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["build_plate", "id"]

    def __str__(self) -> str:
        return f"{self.build_plate} - {self.model_version}"
