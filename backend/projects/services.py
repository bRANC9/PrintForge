"""Business logic for projects, community metadata and AI-assisted content.

The API views and the MCP tools both go through these functions; nothing here
may depend on DRF or an HTTP request (terv.md 25. fejezet). Community features
(terv.md Phase 8) and the AI description/tag fill (terv.md 29.) are additive on
top of the basic project lifecycle.

Build-plate helpers also live here (not in ``slicers/services.py``) because a
``BuildPlate`` belongs to a ``Project`` and the API reaches it through the
project's workspace scope.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Iterable
from typing import Any

from django.db import transaction
from django.db.models import Avg, Count, F, Q
from django.urls import NoReverseMatch, reverse
from django.utils.text import slugify
from pydantic import BaseModel, Field

from accounts.models import User
from workspaces.models import Workspace

from .models import (
    ContentSource,
    ModelDownload,
    Project,
    ProjectShare,
    Rating,
    Tag,
)

__all__ = [
    "PUBLIC_ORDERINGS",
    "ProjectMetadataSuggestion",
    "add_plate_item",
    "build_plates_for_user",
    "create_build_plate",
    "create_project",
    "create_share",
    "generate_project_description",
    "maybe_autofill_project_metadata",
    "project_download_url",
    "project_rating_summary",
    "projects_for_workspace",
    "publish_project",
    "rate_project",
    "record_download",
    "remove_plate_item",
    "revoke_share",
    "search_public_projects",
    "set_project_description",
    "set_project_tags",
    "unpublish_project",
    "unrate_project",
]

logger = logging.getLogger(__name__)

#: Allowed ``ordering`` values for :func:`search_public_projects`.
PUBLIC_ORDERINGS: dict[str, tuple[str, ...]] = {
    "newest": ("-created_at", "-id"),
    "downloads": ("-download_count", "-created_at"),
    "rating": ("-average_rating", "-created_at"),
}


class ProjectMetadataSuggestion(BaseModel):
    """Structured LLM output for the AI description/tag fill (terv.md 29.3)."""

    summary: str = ""
    tags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Basic project lifecycle
# ---------------------------------------------------------------------------


def create_project(
    *,
    workspace: Workspace,
    name: str,
    created_by: User,
    description: str = "",
) -> Project:
    description = description or ""
    return Project.objects.create(
        workspace=workspace,
        name=name,
        description=description,
        # A description supplied by the user is ``manual`` and must never be
        # overwritten by the AI fill (terv.md 29.2).
        description_source=(ContentSource.MANUAL if description.strip() else ContentSource.EMPTY),
        created_by=created_by,
    )


def projects_for_workspace(workspace: Workspace):
    return Project.objects.filter(workspace=workspace)


# ---------------------------------------------------------------------------
# Community: publish / search (terv.md Phase 8)
# ---------------------------------------------------------------------------


def publish_project(project: Project) -> Project:
    """Make ``project`` visible in the community library (idempotent)."""
    if not project.is_public:
        project.is_public = True
        project.save(update_fields=["is_public", "updated_at"])
    return project


def unpublish_project(project: Project) -> Project:
    """Remove ``project`` from the community library (idempotent)."""
    if project.is_public:
        project.is_public = False
        project.save(update_fields=["is_public", "updated_at"])
    return project


def search_public_projects(
    *,
    q: str = "",
    tag_slug: str = "",
    license: str = "",
    ordering: str = "newest",
):
    """Public projects matching the community filters, ordered for the UI.

    ``q`` matches the name/description (icontains), ``tag_slug`` narrows to one
    tag and ``license`` to an exact ``ProjectLicense`` value. ``ordering`` is
    one of ``newest``/``downloads``/``rating`` (unknown values fall back to
    ``newest``). Always annotated with ``average_rating``/``rating_count`` so
    the serializer needs no per-row aggregate query.
    """
    queryset = (
        Project.objects.filter(is_public=True)
        .prefetch_related("tags")
        .annotate(
            average_rating=Avg("ratings__score"),
            rating_count=Count("ratings", distinct=True),
        )
    )
    q = (q or "").strip()
    if q:
        queryset = queryset.filter(Q(name__icontains=q) | Q(description__icontains=q))
    if tag_slug:
        queryset = queryset.filter(tags__slug=tag_slug)
    if license:
        queryset = queryset.filter(license=license)
    order_by = PUBLIC_ORDERINGS.get(ordering, PUBLIC_ORDERINGS["newest"])
    return queryset.order_by(*order_by).distinct()


@transaction.atomic
def record_download(
    project: Project,
    *,
    model_version=None,
    user: User | None = None,
    ip_hash: str = "",
) -> ModelDownload:
    """Log one download and bump ``Project.download_count`` atomically."""
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None
    download = ModelDownload.objects.create(
        project=project,
        model_version=model_version,
        user=user,
        ip_hash=ip_hash or "",
    )
    Project.objects.filter(pk=project.pk).update(download_count=F("download_count") + 1)
    project.refresh_from_db(fields=["download_count"])
    return download


def _get_or_create_tags(names: Iterable[str]) -> list[Tag]:
    """Resolve ``names`` to :class:`Tag` rows (creating missing ones).

    Tags are keyed by slug (``slugify(name)``) rather than by the raw, unique
    ``Tag.name``. Names that normalise to the same slug -- case differences such
    as ``"Box"``/``"box"``, or symbols that slugify drops -- therefore collapse
    onto a single row instead of tripping ``UNIQUE constraint failed:
    projects_tag.slug``. The first spelling wins; the input order is preserved.
    """
    tags: dict[str, Tag] = {}
    for raw in names:
        name = (raw or "").strip()
        if not name:
            continue
        # ``slugify`` can return "" for names without ASCII alphanumerics; fall
        # back to the (unique) name so distinct tags never share one empty slug.
        slug = slugify(name) or name[:80]
        if slug in tags:
            continue
        tag = Tag.objects.filter(slug=slug).first() or Tag.objects.filter(name=name).first()
        if tag is None:
            tag, _ = Tag.objects.get_or_create(slug=slug, defaults={"name": name})
        tags[slug] = tag
    return list(tags.values())


def set_project_tags(project: Project, names: Iterable[str]) -> list[Tag]:
    """Replace the project's tags with ``names`` and mark the source ``manual``."""
    tags = _get_or_create_tags(names)
    project.tags.set(tags)
    if project.tags_source != ContentSource.MANUAL:
        project.tags_source = ContentSource.MANUAL
        project.save(update_fields=["tags_source", "updated_at"])
    return list(project.tags.all())


def set_project_description(project: Project, description: str) -> Project:
    """Set the project's description and record where it came from (terv.md 29.2).

    A user-written value marks ``description_source = manual`` so the AI fill
    never overwrites it; explicitly clearing it to blank marks ``empty`` so the
    AI may legitimately fill it again.
    """
    description = description or ""
    source = ContentSource.MANUAL if description.strip() else ContentSource.EMPTY
    if project.description == description and project.description_source == source:
        return project
    project.description = description
    project.description_source = source
    project.save(update_fields=["description", "description_source", "updated_at"])
    return project


def rate_project(project: Project, user: User, score: int) -> Rating:
    """Create or update ``user``'s 1..5 rating of ``project``."""
    if user is None or not getattr(user, "is_authenticated", False):
        raise ValueError("rating requires an authenticated user")
    score = int(score)
    if not 1 <= score <= 5:
        raise ValueError("score must be between 1 and 5")
    rating, _ = Rating.objects.update_or_create(
        project=project,
        user=user,
        defaults={"score": score},
    )
    return rating


def unrate_project(project: Project, user: User) -> int:
    """Delete ``user``'s rating of ``project``; return the number removed."""
    if user is None or not getattr(user, "is_authenticated", False):
        return 0
    deleted, _ = Rating.objects.filter(project=project, user=user).delete()
    return deleted


def project_rating_summary(project: Project) -> dict[str, Any]:
    """Return ``{"average": float|None, "count": int}`` for ``project``."""
    aggregate = project.ratings.aggregate(average=Avg("score"), count=Count("id"))
    average = aggregate["average"]
    return {
        "average": round(float(average), 2) if average is not None else None,
        "count": int(aggregate["count"] or 0),
    }


# ---------------------------------------------------------------------------
# Sharing (terv.md Phase 7)
# ---------------------------------------------------------------------------


def create_share(
    project: Project,
    *,
    user: User | None = None,
    create_link: bool = False,
    created_by: User | None = None,
    expires_at=None,
) -> ProjectShare:
    """Share ``project`` with ``user`` or mint a public token link.

    ``create_link=True`` creates a token share (``shared_with=None``); otherwise
    ``user`` is required and the ``(project, user)`` pair is upserted so
    re-sharing refreshes ``expires_at`` instead of violating the partial unique
    constraint.
    """
    if create_link:
        token = secrets.token_urlsafe(32)
        while ProjectShare.objects.filter(token=token).exists():
            token = secrets.token_urlsafe(32)
        return ProjectShare.objects.create(
            project=project,
            token=token,
            created_by=created_by,
            expires_at=expires_at,
        )
    if user is None:
        raise ValueError("create_share requires a user unless create_link=True")
    share, _ = ProjectShare.objects.update_or_create(
        project=project,
        shared_with=user,
        defaults={"created_by": created_by, "expires_at": expires_at},
    )
    return share


def revoke_share(share: ProjectShare) -> None:
    """Delete a project share (idempotent).

    Django nulls the primary key after ``delete()``, so a repeated call on the
    same instance must not raise a ``ValueError`` -- there is simply nothing left
    to delete.
    """
    if share.pk is None:
        return
    share.delete()


def project_download_url(project: Project, model_version=None) -> str | None:
    """Return the STL download URL for ``project`` (or one specific version).

    Falls back to the newest version that has an STL when ``model_version`` is
    omitted. Returns ``None`` when no STL artifact exists yet.
    """
    version = model_version
    if version is None:
        version = project.versions.exclude(stl_file="").order_by("-version").first()
    if version is None or not getattr(version.stl_file, "name", ""):
        return None
    try:
        return reverse("version-artifact", kwargs={"pk": version.pk, "kind": "stl"})
    except NoReverseMatch:  # pragma: no cover - URLconf always provides this route
        return version.stl_file.name or None


# ---------------------------------------------------------------------------
# AI description / tags (terv.md 29.)
# ---------------------------------------------------------------------------


def _resolve_provider(provider):
    """Resolve the LLM provider (``None`` -> default, str -> named backend)."""
    from agents.llm import get_provider

    if provider is None:
        return get_provider()
    if isinstance(provider, str):
        return get_provider(provider)
    return provider


def _metadata_prompt(project: Project) -> str:
    """Build the metadata prompt from the project and its latest version."""
    latest = project.versions.order_by("-version").first()
    parts = [f"Project name: {project.name}"]
    if latest is not None:
        if latest.prompt:
            parts.append(f"Latest version prompt: {latest.prompt}")
        if latest.specification_json:
            parts.append(
                "Specification: " + json.dumps(latest.specification_json, ensure_ascii=False)[:4000]
            )
        if latest.validation_json:
            parts.append(
                "Validation: " + json.dumps(latest.validation_json, ensure_ascii=False)[:4000]
            )
        if latest.reference_note:
            parts.append(f"Reference note: {latest.reference_note}")
    parts.append(
        "Write a short, factual technical description (2-3 sentences) and 3-8 "
        "lowercase search tags for this 3D model. Do not use marketing language."
    )
    return "\n".join(parts)


def _metadata_result(
    *,
    applied: bool,
    reason: str,
    description: str | None = None,
    tags: list[str] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "applied": applied,
        "reason": reason,
        "description": description,
        "tags": list(tags or []),
    }
    if error is not None:
        result["error"] = error
    return result


def generate_project_description(
    project: Project,
    *,
    force: bool = False,
    provider=None,
) -> dict[str, Any]:
    """Fill the project's description/tags from the LLM, respecting provenance.

    A ``manual`` field is never overwritten unless ``force=True``. On any LLM
    failure the call returns ``{"applied": False, "error": ...}`` instead of
    raising (terv.md 29.3: the feature silently no-ops when no model is
    available).
    """
    from agents.llm import LLMError

    description_fillable = force or project.description_source != ContentSource.MANUAL
    tags_fillable = force or project.tags_source != ContentSource.MANUAL

    if not description_fillable and not tags_fillable:
        return _metadata_result(applied=False, reason="manual")

    try:
        llm = _resolve_provider(provider)
        result = llm.structured(_metadata_prompt(project), ProjectMetadataSuggestion)
    except LLMError as exc:
        return _metadata_result(applied=False, reason="llm_error", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - this feature must never 500
        logger.exception("Unexpected error generating metadata for project %s", project.pk)
        return _metadata_result(
            applied=False,
            reason="error",
            error=f"{type(exc).__name__}: {exc}",
        )

    summary = str(result.get("summary") or "").strip()
    raw_tags = result.get("tags") or []

    update_fields: list[str] = []
    applied_description: str | None = None
    applied_tags: list[str] = []

    if description_fillable and summary:
        project.description = summary
        project.description_source = ContentSource.AI
        update_fields += ["description", "description_source"]
        applied_description = summary

    if tags_fillable and raw_tags:
        tags = _get_or_create_tags(str(name) for name in raw_tags)
        if tags:
            project.tags.set(tags)
            project.tags_source = ContentSource.AI
            update_fields.append("tags_source")
            applied_tags = [tag.name for tag in tags]

    if not update_fields:
        return _metadata_result(applied=False, reason="nothing_to_fill")

    project.save(update_fields=[*update_fields, "updated_at"])
    return _metadata_result(
        applied=True,
        reason="ok",
        description=applied_description,
        tags=applied_tags,
    )


def maybe_autofill_project_metadata(project: Project) -> dict[str, Any]:
    """Best-effort autofill wrapper, safe to call after a version is created.

    Runs only while the description or the tags are still empty and never
    raises; a missing/unreachable LLM simply yields ``applied=False``.
    """
    if project.description.strip() and project.tags.exists():
        return _metadata_result(applied=False, reason="already_filled")
    try:
        return generate_project_description(project)
    except Exception as exc:  # noqa: BLE001 - callers must never fail because of this
        logger.exception("Autofill failed for project %s", project.pk)
        return _metadata_result(
            applied=False,
            reason="error",
            error=f"{type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# Build plates (terv.md 28.) -- the API reaches these through the project
# ---------------------------------------------------------------------------


def build_plates_for_user(user):
    """Build plates in the workspaces ``user`` is a member of."""
    from slicers.models import BuildPlate

    if user is None or not getattr(user, "is_authenticated", False):
        return BuildPlate.objects.none()
    return (
        BuildPlate.objects.filter(project__workspace__members__user=user)
        .select_related("project", "printer_profile", "created_by")
        .prefetch_related("items")
        .distinct()
    )


def create_build_plate(
    *,
    project: Project,
    name: str,
    created_by: User | None = None,
    printer_profile=None,
    settings_json: dict | None = None,
):
    """Create a build plate inside ``project``."""
    from slicers.models import BuildPlate

    return BuildPlate.objects.create(
        project=project,
        name=name,
        created_by=created_by,
        printer_profile=printer_profile,
        settings_json=settings_json or {},
    )


def add_plate_item(
    build_plate,
    *,
    model_version,
    position_x: float = 0.0,
    position_y: float = 0.0,
    position_z: float = 0.0,
    rotation_z: float = 0.0,
    scale: float = 1.0,
    settings_json: dict | None = None,
):
    """Add ``model_version`` to ``build_plate`` at the given transform."""
    from slicers.models import PlateItem

    if model_version.project_id != build_plate.project_id:
        raise ValueError("model_version does not belong to the build plate's project")
    return PlateItem.objects.create(
        build_plate=build_plate,
        model_version=model_version,
        position_x=position_x,
        position_y=position_y,
        position_z=position_z,
        rotation_z=rotation_z,
        scale=scale,
        settings_json=settings_json or {},
    )


def remove_plate_item(build_plate, item_id) -> bool:
    """Remove one item from ``build_plate``; return whether a row was deleted."""
    from slicers.models import PlateItem

    deleted, _ = PlateItem.objects.filter(build_plate=build_plate, pk=item_id).delete()
    return deleted > 0
