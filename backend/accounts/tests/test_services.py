"""Tests for :mod:`accounts.services` -- the profile statistics aggregation."""

from __future__ import annotations

import pytest
from django.utils import timezone
from factories import (
    ModelDownloadFactory,
    ModelVersionFactory,
    PrinterFactory,
    PrintJobFactory,
    ProjectFactory,
    ProjectPrintFactory,
    ProjectShareFactory,
    RatingFactory,
    SkillFactory,
    UserFactory,
    WorkspaceFactory,
)

from accounts.services import PROFILE_FIELDS, update_profile, user_stats
from printers.models import PrintJob, PrintJobStatus

pytestmark = pytest.mark.django_db


def test_stats_for_a_fresh_user_are_all_zero():
    stats = user_stats(UserFactory())

    assert stats["workspaces"] == {"owned": 0, "joined": 0}
    assert stats["content"]["projects"] == 0
    assert stats["content"]["versions"] == 0
    assert stats["printing"] == {"jobs": 0, "completed": 0, "failed": 0, "active": 0}
    assert stats["reach"]["downloads"] == 0
    assert stats["reach"]["rating_average"] is None


def test_workspaces_owned_and_joined_are_counted_separately():
    user = UserFactory()
    WorkspaceFactory(owner=user)
    WorkspaceFactory()

    stats = user_stats(user)

    assert stats["workspaces"]["owned"] == 1
    # Only the membership created by WorkspaceFactory for the owner.
    assert stats["workspaces"]["joined"] == 1


def test_content_counts_only_what_the_user_authored():
    user = UserFactory()
    ProjectFactory(created_by=user)
    ProjectFactory()  # somebody else's, must not be counted
    ModelVersionFactory(created_by=user)
    SkillFactory(created_by=user)
    ProjectShareFactory(created_by=user, shared_with=UserFactory())

    stats = user_stats(user)

    assert stats["content"]["projects"] == 1
    assert stats["content"]["versions"] == 1
    assert stats["content"]["skills"] == 1
    assert stats["content"]["shares_created"] == 1
    assert stats["content"]["shared_with_me"] == 0


def test_shares_addressed_to_the_user_are_counted():
    user = UserFactory()
    ProjectShareFactory(shared_with=user)

    assert user_stats(user)["content"]["shared_with_me"] == 1


def test_public_project_count_is_a_subset_of_projects():
    user = UserFactory()
    ProjectFactory(created_by=user, is_public=True)
    ProjectFactory(created_by=user, is_public=False)

    stats = user_stats(user)

    assert stats["content"]["projects"] == 2
    assert stats["content"]["public_projects"] == 1


def test_print_jobs_are_split_by_status():
    user = UserFactory()
    PrintJobFactory(created_by=user, status=PrintJobStatus.COMPLETED)
    PrintJobFactory(created_by=user, status=PrintJobStatus.FAILED)
    PrintJobFactory(created_by=user, status=PrintJobStatus.CANCELLED)
    PrintJobFactory(created_by=user, status=PrintJobStatus.QUEUED)
    PrintJobFactory(created_by=user, status=PrintJobStatus.PRINTING)

    stats = user_stats(user)["printing"]

    assert stats["jobs"] == 5
    assert stats["completed"] == 1
    # FAILED and CANCELLED are both "did not finish".
    assert stats["failed"] == 2
    assert stats["active"] == 2


def test_reach_uses_the_denormalised_project_counters():
    user = UserFactory()
    ProjectFactory(created_by=user, download_count=7, print_count=3)
    ProjectFactory(created_by=user, download_count=2, print_count=0)

    stats = user_stats(user)["reach"]

    assert stats["downloads"] == 9
    assert stats["prints"] == 3


def test_reach_event_logs_and_ratings_follow_the_project_author():
    user = UserFactory()
    project = ProjectFactory(created_by=user)
    ModelDownloadFactory(project=project)
    ModelDownloadFactory(project=project)
    ProjectPrintFactory(project=project)
    RatingFactory(project=project, score=5)
    RatingFactory(project=project, score=4)

    stats = user_stats(user)["reach"]

    assert stats["download_events"] == 2
    assert stats["print_events"] == 1
    assert stats["rating_count"] == 2
    assert stats["rating_average"] == 4.5


def test_rating_average_is_none_without_ratings():
    user = UserFactory()
    ProjectFactory(created_by=user)

    assert user_stats(user)["reach"]["rating_average"] is None


def test_other_users_activity_does_not_leak_in():
    user = UserFactory()
    stranger = UserFactory()
    project = ProjectFactory(created_by=stranger)
    ModelDownloadFactory(project=project)
    PrintJobFactory(created_by=stranger)
    ModelVersionFactory(created_by=stranger)
    RatingFactory(project=project, user=user)

    stats = user_stats(user)

    assert stats["content"]["projects"] == 0
    assert stats["content"]["versions"] == 0
    assert stats["printing"]["jobs"] == 0
    # The download belongs to the stranger's project, so it is not our reach.
    assert stats["reach"]["download_events"] == 0
    assert stats["reach"]["rating_count"] == 0
    # ...but the rating the user *gave* is not part of the received stats.
    assert stats["reach"]["rating_average"] is None


def test_activity_series_has_one_zero_filled_bucket_per_month():
    user = UserFactory()

    series = user_stats(user)["activity"]

    assert len(series) == 6
    assert all(row["print_jobs"] == 0 and row["downloads"] == 0 for row in series)
    assert [row["month"] for row in series] == sorted(row["month"] for row in series)


def test_activity_series_buckets_this_month():
    user = UserFactory()
    project = ProjectFactory(created_by=user)
    PrintJobFactory(created_by=user, project=project)
    ModelDownloadFactory(project=project)

    series = user_stats(user)["activity"]

    this_month = timezone.now().strftime("%Y-%m")
    bucket = next(row for row in series if row["month"] == this_month)
    assert bucket["print_jobs"] == 1
    assert bucket["downloads"] == 1


def test_activity_series_ignores_rows_older_than_the_window():
    user = UserFactory()
    project = ProjectFactory(created_by=user)
    old = PrintJobFactory(created_by=user, project=project)
    # Far enough back to be outside the six-month window.
    PrintJob.objects.filter(pk=old.pk).update(
        created_at=timezone.now() - timezone.timedelta(days=400)
    )

    series = user_stats(user)["activity"]

    assert all(row["print_jobs"] == 0 for row in series)


def test_activity_series_ignores_other_users_rows_in_my_workspace():
    user = UserFactory()
    workspace = WorkspaceFactory(owner=user)
    stranger = UserFactory()
    project = ProjectFactory(workspace=workspace, created_by=stranger)
    PrintJobFactory(created_by=stranger, project=project)

    series = user_stats(user)["activity"]

    assert all(row["print_jobs"] == 0 for row in series)


def test_update_profile_saves_only_the_editable_fields():
    user = UserFactory(display_name="Old", email="old@example.com")

    update_profile(user, display_name="New", email="new@example.com", username="hacked")

    user.refresh_from_db()
    assert user.display_name == "New"
    assert user.email == "new@example.com"
    # username is not in PROFILE_FIELDS, so it must be untouched.
    assert user.username != "hacked"


def test_update_profile_ignores_unknown_keys():
    user = UserFactory()

    update_profile(user, is_staff=True, is_superuser=True)

    user.refresh_from_db()
    assert user.is_staff is False
    assert user.is_superuser is False


def test_profile_fields_exclude_username():
    assert "username" not in PROFILE_FIELDS
    assert "password" not in PROFILE_FIELDS
    assert "is_staff" not in PROFILE_FIELDS


def test_update_profile_with_no_fields_is_a_noop():
    user = UserFactory(display_name="Same")

    update_profile(user)

    user.refresh_from_db()
    assert user.display_name == "Same"


def test_own_projects_are_reported_even_after_leaving_the_workspace():
    """Attribution comes from ``created_by``, not from current membership."""
    from workspaces.models import WorkspaceMember

    user = UserFactory()
    workspace = WorkspaceFactory(owner=user)
    ProjectFactory(workspace=workspace, created_by=user)
    WorkspaceMember.objects.filter(user=user, workspace=workspace).delete()

    stats = user_stats(user)

    assert stats["content"]["projects"] == 1
    assert stats["workspaces"]["joined"] == 0


def test_printer_is_not_required_for_job_counting():
    """Printers are a global registry with no owner, so jobs attribute to the user."""
    user = UserFactory()
    PrintJobFactory(created_by=user, printer=PrinterFactory())

    assert user_stats(user)["printing"]["jobs"] == 1
