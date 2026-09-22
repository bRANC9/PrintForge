"""Projects, community metadata and sharing (terv.md 21-22., 29. fejezet).

A project belongs to a workspace; the community fields (``is_public``,
``license``, tags, ratings, downloads) are additive and only meaningful once
the owner opts the project into the public library.
"""

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.text import slugify


class ContentSource(models.TextChoices):
    """Honnan származik egy szöveges mező tartalma (terv.md 29. fejezet).

    ``manual`` értéket az AI soha nem ír felül, ``ai``/``empty`` esetén a
    generált javaslat feltölthető.
    """

    MANUAL = "manual", "Manual"
    AI = "ai", "AI"
    EMPTY = "empty", "Empty"


class ProjectLicense(models.TextChoices):
    """Licenc a nyilvános közösségi modellekhez (Phase 8)."""

    CC0 = "CC0-1.0", "CC0 1.0"
    CC_BY = "CC-BY-4.0", "CC BY 4.0"
    CC_BY_SA = "CC-BY-SA-4.0", "CC BY-SA 4.0"
    MIT = "MIT", "MIT"
    APACHE = "Apache-2.0", "Apache 2.0"
    GPL3 = "GPL-3.0", "GPL 3.0"
    PROPRIETARY = "proprietary", "Proprietary"


class Tag(models.Model):
    """Szabad szavas címke a közösségi kereséshez (Phase 8)."""

    name = models.CharField(max_length=60, unique=True)
    slug = models.SlugField(max_length=80, unique=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


class Project(models.Model):
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="projects",
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    is_public = models.BooleanField(default=False, db_index=True)
    license = models.CharField(
        max_length=50,
        choices=ProjectLicense.choices,
        blank=True,
        default="",
    )
    tags = models.ManyToManyField(
        "projects.Tag",
        blank=True,
        related_name="projects",
    )
    download_count = models.PositiveIntegerField(default=0)
    description_source = models.CharField(
        max_length=10,
        choices=ContentSource.choices,
        default=ContentSource.EMPTY,
    )
    tags_source = models.CharField(
        max_length=10,
        choices=ContentSource.choices,
        default=ContentSource.EMPTY,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_projects",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "name"],
                name="uniq_project_name_per_workspace",
            )
        ]

    def __str__(self) -> str:
        return self.name


class ProjectShare(models.Model):
    """Projekt megosztása userrel vagy nyilvános tokennel (Phase 7).

    Egy sor ``shared_with=None`` és nem üres ``token`` értékkel egy nyilvános
    link; konkrét userhez kötött sor esetén a ``token`` üres.
    """

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="shares",
    )
    shared_with = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="shared_projects",
    )
    token = models.CharField(max_length=64, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_project_shares",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "shared_with"],
                condition=models.Q(shared_with__isnull=False),
                name="uniq_project_share_user",
            ),
            models.UniqueConstraint(
                fields=["token"],
                condition=~models.Q(token=""),
                name="uniq_project_share_token",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.project} -> {self.shared_with or self.token}"


class Rating(models.Model):
    """Egy felhasználó 1-5 közötti értékelése egy projekthez (Phase 8)."""

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="ratings",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ratings",
    )
    score = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "user"],
                name="uniq_project_rating_user",
            )
        ]

    def __str__(self) -> str:
        return f"{self.project} ({self.score}/5)"


class ModelDownload(models.Model):
    """Letöltési napló a közösségi statisztikákhoz (Phase 8)."""

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="downloads",
    )
    model_version = models.ForeignKey(
        "designs.ModelVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="downloads",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="downloads",
    )
    ip_hash = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Download {self.project_id} @ {self.created_at}"
