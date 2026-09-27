"""User services.

Business logic lives here, not in views, so the same functions can be called
by the DRF API and (later) by MCP tools.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from designs.models import ModelVersion
from printers.models import PrintJob, PrintJobStatus
from projects.models import ModelDownload, Project, ProjectPrint, ProjectShare, Rating
from skills.models import Skill
from workspaces.models import Workspace, WorkspaceMember

User = get_user_model()

__all__ = [
    "PROFILE_FIELDS",
    "ACTIVITY_MONTHS",
    "create_user",
    "get_user_by_email",
    "update_profile",
    "user_stats",
]

#: Fields the profile endpoint is allowed to change. ``username`` is
#: deliberately absent: it is the login identity, so renaming it deserves its
#: own confirmation flow rather than riding along on a profile save.
PROFILE_FIELDS = ("display_name", "email", "first_name", "last_name")

#: How many months of activity the profile page charts.
ACTIVITY_MONTHS = 6


def create_user(*, username: str, email: str, password: str, **extra) -> User:
    return User.objects.create_user(username=username, email=email, password=password, **extra)


def get_user_by_email(email: str) -> User | None:
    return User.objects.filter(email__iexact=email).first()


def update_profile(user: User, **fields) -> User:
    """Apply a partial profile update and save only the touched columns.

    Unknown keys are ignored rather than raising, so the caller can forward a
    whole validated payload without pre-filtering it.
    """
    touched = [name for name in PROFILE_FIELDS if name in fields]
    for name in touched:
        setattr(user, name, fields[name])
    if touched:
        user.save(update_fields=touched)
    return user


def _monthly_counts(queryset, field: str, since) -> dict[tuple[int, int], int]:
    """Count ``queryset`` per calendar month, keyed by ``(year, month)``.

    Grouping happens in SQL; the result is keyed to ``(year, month)`` rather
    than to the raw ``TruncMonth`` datetime so the caller can match buckets
    without repeating the truncation.
    """
    rows = (
        queryset.filter(created_at__gte=since)
        .annotate(month=TruncMonth(field))
        .values_list("month")
        .annotate(total=Count("id"))
    )
    return {(row[0].year, row[0].month): row[1] for row in rows if row[0] is not None}


def _activity_series(user: User, months: int = ACTIVITY_MONTHS) -> list[dict[str, Any]]:
    """Per-month print-job and download counts for the trailing ``months``.

    Empty months are materialised in Python instead of being left out of the
    result, so a quiet month still renders as a zero-height bar rather than
    silently shifting the rest of the series left.
    """
    now = timezone.now()
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for _ in range(months - 1):
        first = (first - timedelta(days=1)).replace(day=1)

    jobs = _monthly_counts(PrintJob.objects.filter(created_by=user), "created_at", first)
    downloads = _monthly_counts(
        ModelDownload.objects.filter(project__created_by=user), "created_at", first
    )

    series = []
    cursor = first
    for _ in range(months):
        key = (cursor.year, cursor.month)
        series.append(
            {
                "month": f"{cursor.year:04d}-{cursor.month:02d}",
                "print_jobs": jobs.get(key, 0),
                "downloads": downloads.get(key, 0),
            }
        )
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    return series


def user_stats(user: User) -> dict[str, Any]:
    """Aggregated activity counters for one user.

    Every figure is a single ``.count()`` or ``.aggregate()`` -- no rows are
    loaded, so the whole payload is a fixed, small number of queries.

    Attribution comes from the explicit ``created_by`` foreign keys rather than
    from workspace membership: a user should keep seeing what they authored even
    in a workspace they have since left. ``print_count``/``download_count`` are
    the denormalised counters already shown in the UI, so "reach" agrees with
    the rest of the app instead of re-counting the log tables.
    """
    my_projects = Project.objects.filter(created_by=user)
    my_jobs = PrintJob.objects.filter(created_by=user)

    project_totals = my_projects.aggregate(
        total=Count("id"),
        public=Count("id", filter=Q(is_public=True)),
        downloads=Sum("download_count"),
        prints=Sum("print_count"),
    )
    rating_totals = Rating.objects.filter(project__created_by=user).aggregate(
        average=Avg("score"),
        count=Count("id"),
    )
    job_totals = my_jobs.aggregate(
        total=Count("id"),
        completed=Count("id", filter=Q(status=PrintJobStatus.COMPLETED)),
        failed=Count("id", filter=Q(status__in=(PrintJobStatus.FAILED, PrintJobStatus.CANCELLED))),
        active=Count(
            "id",
            filter=Q(
                status__in=(
                    PrintJobStatus.QUEUED,
                    PrintJobStatus.PREPARING,
                    PrintJobStatus.SLICING,
                    PrintJobStatus.READY,
                    PrintJobStatus.PRINTING,
                    PrintJobStatus.PAUSED,
                )
            ),
        ),
    )

    average = rating_totals["average"]
    return {
        "workspaces": {
            "owned": Workspace.objects.filter(owner=user).count(),
            # The owner also has an explicit OWNER membership row, so this
            # includes the workspaces counted under "owned".
            "joined": WorkspaceMember.objects.filter(user=user).count(),
        },
        "content": {
            "projects": int(project_totals["total"] or 0),
            "public_projects": int(project_totals["public"] or 0),
            "versions": ModelVersion.objects.filter(created_by=user).count(),
            "skills": Skill.objects.filter(created_by=user).count(),
            "shares_created": ProjectShare.objects.filter(created_by=user).count(),
            "shared_with_me": ProjectShare.objects.filter(shared_with=user).count(),
        },
        "reach": {
            # The denormalised project counters, summed.
            "downloads": int(project_totals["downloads"] or 0),
            "prints": int(project_totals["prints"] or 0),
            "download_events": ModelDownload.objects.filter(project__created_by=user).count(),
            "print_events": ProjectPrint.objects.filter(project__created_by=user).count(),
            "rating_count": int(rating_totals["count"] or 0),
            "rating_average": round(float(average), 2) if average is not None else None,
        },
        "printing": {
            "jobs": int(job_totals["total"] or 0),
            "completed": int(job_totals["completed"] or 0),
            "failed": int(job_totals["failed"] or 0),
            "active": int(job_totals["active"] or 0),
        },
        "activity": _activity_series(user),
    }
