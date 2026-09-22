"""Printer registry and print queue (terv.md 13-14. fejezet).

The core app must not depend on a specific printer protocol. Concrete backends
(CrealityK2, Moonraker, OctoPrint, Bambu) are adapters behind a
``PrinterBackend`` interface in ``printers/services.py``
(printer-integration's scope); this module only stores data.
"""

from django.conf import settings
from django.db import models


class PrintJobStatus(models.TextChoices):
    QUEUED = "QUEUED", "Queued"
    PREPARING = "PREPARING", "Preparing"
    SLICING = "SLICING", "Slicing"
    READY = "READY", "Ready"
    PRINTING = "PRINTING", "Printing"
    PAUSED = "PAUSED", "Paused"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"
    CANCELLED = "CANCELLED", "Cancelled"


def print_job_gcode_path(instance: "PrintJob", filename: str) -> str:
    return f"print_jobs/{instance.project_id}/{filename}"


class Printer(models.Model):
    """A physical printer reachable through a ``PrinterBackend`` adapter."""

    name = models.CharField(max_length=200)
    backend = models.CharField(max_length=50, blank=True)
    host = models.CharField(max_length=255, blank=True)
    #: Optional API key/token for the backend's local API (e.g. Moonraker's
    #: ``[authorization]``). Blank means "trusted_clients / no auth". Adapters
    #: must never log this value.
    api_key = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class PrintJob(models.Model):
    """One item in the print queue (terv.md 14. fejezet).

    ``priority`` is a plain integer; higher values are dispatched first and
    ties are broken by ``created_at`` (FIFO).
    """

    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        related_name="print_jobs",
    )
    model_version = models.ForeignKey(
        "designs.ModelVersion",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="print_jobs",
    )
    build_plate = models.ForeignKey(
        "slicers.BuildPlate",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="print_jobs",
    )
    printer = models.ForeignKey(
        Printer,
        on_delete=models.PROTECT,
        related_name="print_jobs",
    )
    filament = models.ForeignKey(
        "slicers.FilamentProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="print_jobs",
    )
    slicer_profile = models.ForeignKey(
        "slicers.ProcessProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="print_jobs",
    )
    printer_profile = models.ForeignKey(
        "slicers.PrinterProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="print_jobs",
    )
    slicing_json = models.JSONField(default=dict, blank=True)
    gcode = models.FileField(upload_to=print_job_gcode_path, blank=True)
    status = models.CharField(
        max_length=20,
        choices=PrintJobStatus.choices,
        default=PrintJobStatus.QUEUED,
    )
    priority = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_print_jobs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-priority", "created_at"]
        constraints = [
            # Egy job vagy egy ModelVersion-re, vagy egy BuildPlate-re mutat.
            models.CheckConstraint(
                condition=models.Q(model_version__isnull=False)
                | models.Q(build_plate__isnull=False),
                name="print_job_has_target",
            )
        ]

    def __str__(self) -> str:
        return f"PrintJob #{self.pk} ({self.status})"
