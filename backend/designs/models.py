from django.conf import settings
from django.db import models


def model_artifact_path(instance: "ModelVersion", filename: str) -> str:
    return f"projects/{instance.project_id}/v{instance.version}/{filename}"


class ModelVersionOrigin(models.TextChoices):
    """How a :class:`ModelVersion` came to be (docs/version-history-controls.md 1.)."""

    GENERATE = "generate", "Generate"
    ANNOTATION = "annotation", "Annotation"
    REGENERATE = "regenerate", "Regenerate"
    MANUAL = "manual", "Manual"


class ModelVersion(models.Model):
    """One immutable version of a project's generated model.

    Files (scad/stl/glb/preview) are stored via the storage backend; the DB
    only keeps the relative paths.
    """

    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version = models.PositiveIntegerField()
    prompt = models.TextField(blank=True)
    specification_json = models.JSONField(default=dict, blank=True)
    validation_json = models.JSONField(default=dict, blank=True)

    # Edit-chain source: the version this one was derived from via a
    # visual-prompt edit (docs/visual-editing.md 3.2).
    parent_version = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="derived_versions",
    )
    # Visual-prompt input (the annotation payload) stored verbatim so the
    # edit can be reproduced and its provenance audited.
    annotations_json = models.JSONField(default=list, blank=True)
    # Explicit provenance for the version list (regenerate/edit/manual).
    origin = models.CharField(
        max_length=16,
        choices=ModelVersionOrigin.choices,
        default=ModelVersionOrigin.GENERATE,
    )

    scad_file = models.FileField(upload_to=model_artifact_path, blank=True)
    stl_file = models.FileField(upload_to=model_artifact_path, blank=True)
    glb_file = models.FileField(upload_to=model_artifact_path, blank=True)
    preview_image = models.ImageField(upload_to=model_artifact_path, blank=True)
    # Opcionális referencia fotó a prompt mellett (terv.md 27. fejezet).
    reference_image = models.ImageField(upload_to=model_artifact_path, blank=True)
    reference_note = models.TextField(blank=True, default="")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_versions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["project", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "version"],
                name="uniq_model_version_per_project",
            )
        ]

    def __str__(self) -> str:
        return f"{self.project} v{self.version}"
