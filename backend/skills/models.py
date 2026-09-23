"""Reusable generation recipes (skills, docs/skills.md 2. fejezet).

A skill is structured data plus textual guidance, never OpenSCAD/Python code.
Two kinds share one schema: ``guidance`` (prompt guidance + machine-checkable
constraints) and ``template`` (names a built-in CAD generator via
``template_key``). The built-in phone holder is modelled as a ``template``
skill instead of a separate code path.
"""

from django.conf import settings
from django.db import models
from django.utils.text import slugify


class SkillKind(models.TextChoices):
    GUIDANCE = "guidance", "Guidance"
    TEMPLATE = "template", "Template"


class Skill(models.Model):
    """A named, reusable recipe that guides the generation of an object class.

    ``workspace=None`` + ``is_builtin=True`` is a global seed skill;
    ``workspace=<ws>`` is a workspace-private skill. See docs/skills.md.
    """

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    description = models.TextField(blank=True)

    kind = models.CharField(
        max_length=16,
        choices=SkillKind.choices,
        default=SkillKind.GUIDANCE,
    )
    template_key = models.CharField(max_length=64, blank=True, default="")
    object_kind = models.CharField(max_length=64, blank=True, default="")

    # Guidance: injected into the Planner/Editor prompt (bounded).
    guidance = models.TextField(blank=True)
    # Structured defaults (a validated subset of ModelSpecification).
    defaults_json = models.JSONField(default=dict, blank=True)
    # Machine checks the Validator enforces (not only prompt suggestions).
    constraints_json = models.JSONField(default=dict, blank=True)
    # Keywords/tags for deterministic auto-matching.
    tags = models.ManyToManyField("projects.Tag", blank=True, related_name="skills")

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="skills",
    )
    is_builtin = models.BooleanField(default=False)
    is_public = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_skills",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)
