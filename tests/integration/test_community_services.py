"""Service-level tests for the Phase 8 community features (terv.md Phase 8).

These exercise ``projects.services`` directly (no HTTP), so they complement the
endpoint-focused tests and pin down the behaviours the MCP tools and the API
must share: publish/unpublish idempotency, search filters/ordering, tag
replacement with provenance, the rating aggregate, the atomic download counter
and the sharing rules.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone
from factories import ModelVersionFactory, ProjectFactory, UserFactory

from projects.models import ContentSource, ModelDownload, ProjectShare, Rating
from projects.services import (
    create_share,
    project_download_url,
    project_rating_summary,
    publish_project,
    rate_project,
    record_download,
    revoke_share,
    search_public_projects,
    set_project_tags,
    unpublish_project,
    unrate_project,
)

pytestmark = pytest.mark.django_db


def _public(name: str, **kwargs):
    return ProjectFactory(name=name, is_public=True, **kwargs)


# ---------------------------------------------------------------------------
# Publish / unpublish
# ---------------------------------------------------------------------------


def test_publish_and_unpublish_are_idempotent():
    project = ProjectFactory(is_public=False)

    publish_project(project)
    project.refresh_from_db()
    assert project.is_public is True

    publish_project(project)
    project.refresh_from_db()
    assert project.is_public is True

    unpublish_project(project)
    project.refresh_from_db()
    assert project.is_public is False

    unpublish_project(project)
    project.refresh_from_db()
    assert project.is_public is False


# ---------------------------------------------------------------------------
# Public search
# ---------------------------------------------------------------------------


def test_search_only_returns_public_rows_with_rating_annotations():
    public = _public("Public")
    hidden = ProjectFactory(name="Hidden", is_public=False)
    rate_project(public, UserFactory(), 4)
    rate_project(public, UserFactory(), 2)

    results = list(search_public_projects())

    assert [p.pk for p in results] == [public.pk]
    assert hidden.pk not in {p.pk for p in results}
    assert results[0].average_rating == 3.0
    assert results[0].rating_count == 2


def test_search_matches_name_or_description():
    by_name = _public("Alpha Bracket")
    by_description = ProjectFactory(name="Other", description="a sturdy bracket", is_public=True)
    _public("Unrelated")

    found = {p.pk for p in search_public_projects(q="bracket")}

    assert found == {by_name.pk, by_description.pk}
    assert [p.pk for p in search_public_projects(q="  ")] != []


def test_search_filters_by_tag_slug_and_license():
    tagged = _public("Tagged", license="MIT")
    set_project_tags(tagged, ["Widget"])
    other = _public("Other", license="Apache-2.0")

    by_tag = [p.pk for p in search_public_projects(tag_slug="widget")]
    by_license = [p.pk for p in search_public_projects(license="MIT")]

    assert by_tag == [tagged.pk]
    assert by_license == [tagged.pk]
    assert other.pk not in by_tag


def test_search_ordering_and_unknown_fallback():
    now = timezone.now()
    low = _public("Low")
    high = _public("High")
    low.created_at = now - timedelta(days=1)
    low.save(update_fields=["created_at"])
    high.created_at = now
    high.save(update_fields=["created_at"])

    record_download(high)
    record_download(high)
    rate_project(low, UserFactory(), 5)

    newest = [p.pk for p in search_public_projects(ordering="newest")]
    by_downloads = [p.pk for p in search_public_projects(ordering="downloads")]
    by_rating = [p.pk for p in search_public_projects(ordering="rating")]
    unknown = [p.pk for p in search_public_projects(ordering="does-not-exist")]

    assert newest == [high.pk, low.pk]
    assert by_downloads == [high.pk, low.pk]
    assert by_rating == [low.pk, high.pk]
    assert unknown == newest  # unknown values fall back to newest


def test_rating_ordering_sorts_unrated_projects_last_on_every_backend():
    # Regression: a plain ``-average_rating`` puts NULLs *first* on PostgreSQL
    # but *last* on SQLite, so the ordering must be explicit.
    from projects.services import PUBLIC_ORDERINGS

    expression = PUBLIC_ORDERINGS["rating"][0]
    assert expression.descending is True
    assert expression.nulls_last is True


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


def test_set_project_tags_replaces_and_marks_manual():
    project = ProjectFactory()
    set_project_tags(project, ["Widget", "Gadget"])

    project.refresh_from_db()
    assert project.tags_source == ContentSource.MANUAL
    assert list(project.tags.order_by("name").values_list("name", flat=True)) == [
        "Gadget",
        "Widget",
    ]

    # Replacing drops the previous set and ignores repeated names.
    set_project_tags(project, ["Only", "Only"])
    assert list(project.tags.values_list("name", flat=True)) == ["Only"]


def test_set_project_tags_collapses_names_that_share_a_slug():
    project = ProjectFactory()

    # Case variants normalise to the same slug: the first spelling wins and no
    # UNIQUE(slug) violation is raised.
    set_project_tags(project, ["Box", "box", "BOX"])
    assert list(project.tags.values_list("name", flat=True)) == ["Box"]

    # ``slugify`` drops symbols, so "C++" and "C" also collapse onto "c".
    set_project_tags(project, ["C++", "C"])
    assert list(project.tags.values_list("name", flat=True)) == ["C++"]


# ---------------------------------------------------------------------------
# Ratings
# ---------------------------------------------------------------------------


def test_rate_project_upserts_and_validates():
    project = ProjectFactory()
    user = UserFactory()

    rate_project(project, user, 3)
    rate_project(project, user, 5)

    assert Rating.objects.filter(project=project, user=user).count() == 1
    assert project_rating_summary(project) == {"average": 5.0, "count": 1}

    with pytest.raises(ValueError):
        rate_project(project, user, 0)
    with pytest.raises(ValueError):
        rate_project(project, user, 6)
    with pytest.raises(ValueError):
        rate_project(project, AnonymousUser(), 3)


def test_unrate_project_is_scoped_and_anonymous_safe():
    project = ProjectFactory()
    mine = UserFactory()
    theirs = UserFactory()
    rate_project(project, mine, 4)
    rate_project(project, theirs, 2)

    assert unrate_project(project, mine) == 1
    assert unrate_project(project, mine) == 0  # already gone
    assert unrate_project(project, AnonymousUser()) == 0
    assert project_rating_summary(project) == {"average": 2.0, "count": 1}


def test_project_rating_summary_empty_project():
    assert project_rating_summary(ProjectFactory()) == {"average": None, "count": 0}


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------


def test_record_download_bumps_the_counter_and_logs_the_row():
    project = ProjectFactory()
    version = ModelVersionFactory(project=project)
    user = UserFactory()

    download = record_download(project, model_version=version, user=user, ip_hash="abc")

    project.refresh_from_db()
    assert project.download_count == 1
    assert download.project_id == project.id
    assert download.model_version_id == version.id
    assert download.user_id == user.id
    assert download.ip_hash == "abc"

    record_download(project)
    project.refresh_from_db()
    assert project.download_count == 2
    assert ModelDownload.objects.filter(project=project).count() == 2


def test_record_download_treats_anonymous_as_no_user():
    project = ProjectFactory()

    download = record_download(project, user=AnonymousUser(), ip_hash="")

    assert download.user is None
    assert download.ip_hash == ""


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------


def test_create_share_upserts_the_user_share():
    project = ProjectFactory()
    creator = UserFactory()
    target = UserFactory()

    first = create_share(project, user=target, created_by=creator)
    second = create_share(project, user=target, created_by=creator)

    assert first.pk == second.pk
    assert ProjectShare.objects.filter(project=project, shared_with=target).count() == 1


def test_create_share_link_tokens_are_unique_and_required_user_is_enforced():
    project = ProjectFactory()

    first = create_share(project, create_link=True)
    second = create_share(project, create_link=True)

    assert first.token and second.token
    assert first.token != second.token
    assert first.shared_with is None

    with pytest.raises(ValueError):
        create_share(project, user=None)


def test_revoke_share_is_idempotent_on_the_same_instance():
    project = ProjectFactory()
    share = create_share(project, create_link=True)
    share_pk = share.pk

    revoke_share(share)

    assert not ProjectShare.objects.filter(pk=share_pk).exists()

    # Django nulls the pk on delete; a second call on the same instance must
    # not raise (regression: it used to raise ValueError).
    revoke_share(share)
    assert ProjectShare.objects.filter(pk=share_pk).first() is None


# ---------------------------------------------------------------------------
# Download URL
# ---------------------------------------------------------------------------


def test_project_download_url_returns_none_without_an_stl():
    project = ProjectFactory()
    ModelVersionFactory(project=project)

    assert project_download_url(project) is None


def test_project_download_url_prefers_the_newest_version_with_an_stl():
    project = ProjectFactory()
    old = ModelVersionFactory(project=project, version=1)
    old.stl_file.name = f"projects/{project.pk}/v1/model.stl"
    old.save(update_fields=["stl_file"])
    new = ModelVersionFactory(project=project, version=2)
    new.stl_file.name = f"projects/{project.pk}/v2/model.stl"
    new.save(update_fields=["stl_file"])

    url = project_download_url(project)

    assert url == f"/api/v1/versions/{new.pk}/artifact/stl/"
    assert project_download_url(project, old) == f"/api/v1/versions/{old.pk}/artifact/stl/"
