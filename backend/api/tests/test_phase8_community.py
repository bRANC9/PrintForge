"""Focused tests for the Phase 8 community + #27/#28/#29 API surface.

Covers only the new endpoints/services added by ``api-dev``; the broad
regression suites live under ``tests/`` (owned by ``qa-tests``).
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from designs.services import create_next_version
from printers.models import Printer
from projects.models import ContentSource
from projects.services import (
    create_project,
    generate_project_description,
    project_rating_summary,
    search_public_projects,
)
from slicers.models import BuildPlate
from workspaces.models import WorkspaceRole
from workspaces.services import add_member, create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()


def make_user(username, **extra):
    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password="pw", **extra
    )


def auth(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def owner():
    return make_user("owner")


@pytest.fixture
def outsider():
    return make_user("outsider")


@pytest.fixture
def workspace(owner):
    return create_workspace(name="Lab", owner=owner)


@pytest.fixture
def project(workspace, owner):
    return create_project(workspace=workspace, name="Holder", created_by=owner)


@pytest.fixture
def version(project, owner):
    return create_next_version(project=project, prompt="make it", created_by=owner)


# ---------------------------------------------------------------------------
# Community: publish / search / tags
# ---------------------------------------------------------------------------


def test_publish_unpublish_and_anonymous_community_read(workspace, project, owner):
    client = auth(owner)
    published = client.post(f"/api/v1/projects/{project.pk}/publish/")
    assert published.status_code == 200
    assert published.json()["is_public"] is True

    listing = APIClient().get("/api/v1/community/projects/")
    assert listing.status_code == 200
    assert [row["id"] for row in listing.json()["results"]] == [project.pk]

    assert client.post(f"/api/v1/projects/{project.pk}/unpublish/").json()["is_public"] is False
    assert APIClient().get("/api/v1/community/projects/").json()["results"] == []


def test_community_filters_and_rating_ordering(workspace, owner):
    from projects.models import Project

    a = create_project(workspace=workspace, name="Alpha Bracket", created_by=owner)
    b = create_project(workspace=workspace, name="Beta Mount", created_by=owner)
    Project.objects.filter(pk__in=[a.pk, b.pk]).update(is_public=True)
    client = auth(owner)
    client.post(f"/api/v1/projects/{a.pk}/rate/", {"score": 5}, format="json")

    by_name = APIClient().get("/api/v1/community/projects/", {"q": "bracket"})
    assert [row["id"] for row in by_name.json()["results"]] == [a.pk]

    by_rating = APIClient().get("/api/v1/community/projects/", {"ordering": "rating"})
    assert by_rating.json()["results"][0]["rating"] == {"average": 5.0, "count": 1}

    tagged = APIClient().get("/api/v1/community/projects/", {"tag": "nope"})
    assert tagged.json()["results"] == []


def test_project_tags_are_written_as_names_and_read_as_slugs(workspace, owner):
    client = auth(owner)
    created = client.post(
        "/api/v1/projects/",
        {
            "workspace": workspace.pk,
            "name": "Tagged",
            "tags": ["Phone Holder", "Desk"],
            "license": "MIT",
        },
        format="json",
    )
    assert created.status_code == 201
    body = created.json()
    assert sorted(body["tags"]) == ["desk", "phone-holder"]
    assert body["license"] == "MIT"
    assert body["tags_source"] == ContentSource.MANUAL

    project_id = body["id"]
    patched = client.patch(f"/api/v1/projects/{project_id}/", {"tags": ["Only One"]}, format="json")
    assert patched.json()["tags"] == ["only-one"]


def test_tag_catalogue_is_public(workspace, owner):
    auth(owner).post(
        "/api/v1/projects/",
        {"workspace": workspace.pk, "name": "Tagged", "tags": ["Widget"]},
        format="json",
    )
    response = APIClient().get("/api/v1/tags/")
    assert response.status_code == 200
    assert [row["slug"] for row in response.json()["results"]] == ["widget"]


# ---------------------------------------------------------------------------
# Ratings / downloads / sharing
# ---------------------------------------------------------------------------


def test_rate_and_unrate_project(workspace, project, owner, outsider):
    client = auth(owner)
    rated = client.post(f"/api/v1/projects/{project.pk}/rate/", {"score": 4}, format="json")
    assert rated.status_code == 200
    assert rated.json() == {"summary": {"average": 4.0, "count": 1}, "mine": 4}

    # Re-rating upserts instead of adding a second row.
    client.post(f"/api/v1/projects/{project.pk}/rate/", {"score": 5}, format="json")
    assert project_rating_summary(project) == {"average": 5.0, "count": 1}

    rater = make_user("rater")
    add_member(workspace=workspace, user=rater, role=WorkspaceRole.MEMBER)
    auth(rater).post(f"/api/v1/projects/{project.pk}/rate/", {"score": 4}, format="json")
    assert project_rating_summary(project) == {"average": 4.5, "count": 2}

    removed = client.delete(f"/api/v1/projects/{project.pk}/rate/")
    assert removed.json()["summary"] == {"average": 4.0, "count": 1}

    # Non-members never reach the project at all.
    assert (
        auth(outsider)
        .post(f"/api/v1/projects/{project.pk}/rate/", {"score": 5}, format="json")
        .status_code
        == 404
    )


def test_download_records_and_returns_the_stl_url(project, version, owner):
    version.stl_file.name = f"projects/{project.pk}/v{version.version}/model.stl"
    version.save(update_fields=["stl_file"])

    response = auth(owner).get(f"/api/v1/projects/{project.pk}/download/")

    assert response.status_code == 200
    body = response.json()
    assert body["url"] == f"/api/v1/versions/{version.pk}/artifact/stl/"
    assert body["download_count"] == 1
    project.refresh_from_db()
    assert project.download_count == 1


def test_share_create_list_and_revoke(project, owner, outsider):
    client = auth(owner)

    link = client.post(
        f"/api/v1/projects/{project.pk}/share/", {"create_link": True}, format="json"
    )
    assert link.status_code == 201
    assert link.json()["token"]

    user_share = client.post(
        f"/api/v1/projects/{project.pk}/share/", {"user": outsider.pk}, format="json"
    )
    assert user_share.status_code == 201

    listed = client.get(f"/api/v1/projects/{project.pk}/share/")
    assert len(listed.json()) == 2

    revoked = client.delete(f"/api/v1/projects/{project.pk}/shares/{link.json()['id']}/")
    assert revoked.status_code == 204
    assert len(client.get(f"/api/v1/projects/{project.pk}/share/").json()) == 1


# ---------------------------------------------------------------------------
# AI description / tags (terv.md 29.)
# ---------------------------------------------------------------------------


class FakeProvider:
    def __init__(self, result):
        self._result = result

    def structured(self, prompt, schema, **kwargs):
        return self._result


def test_description_ai_endpoint_fills_empty_fields(project, owner, monkeypatch):
    monkeypatch.setattr(
        "agents.llm.get_provider",
        lambda *a, **k: FakeProvider({"summary": "A sturdy wall bracket.", "tags": ["Bracket"]}),
    )
    response = auth(owner).post(f"/api/v1/projects/{project.pk}/description-ai/")

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert body["description"] == "A sturdy wall bracket."
    assert body["project"]["tags"] == ["bracket"]
    assert body["project"]["description_source"] == ContentSource.AI


def test_generate_never_overwrites_manual_fields(project):
    project.description = "Hand written"
    project.description_source = ContentSource.MANUAL
    project.tags_source = ContentSource.EMPTY
    project.save()

    provider = FakeProvider({"summary": "AI text", "tags": ["ai-tag"]})
    result = generate_project_description(project, provider=provider)

    assert result["applied"] is True
    project.refresh_from_db()
    assert project.description == "Hand written"
    assert project.description_source == ContentSource.MANUAL
    assert project.tags_source == ContentSource.AI
    assert list(project.tags.values_list("slug", flat=True)) == ["ai-tag"]


def test_generate_skips_when_everything_is_manual(project):
    project.description_source = ContentSource.MANUAL
    project.tags_source = ContentSource.MANUAL
    project.save()

    called = False

    class BoomProvider:
        def structured(self, *a, **k):
            nonlocal called
            called = True
            raise AssertionError("must not call the LLM")

    result = generate_project_description(project, provider=BoomProvider())

    assert result["applied"] is False
    assert result["reason"] == "manual"
    assert called is False


def test_generate_returns_gracefully_on_llm_error(project):
    from agents.llm import LLMError

    class FailingProvider:
        def structured(self, *a, **k):
            raise LLMError("ollama down")

    result = generate_project_description(project, provider=FailingProvider())

    assert result == {
        "applied": False,
        "reason": "llm_error",
        "error": "ollama down",
        "description": None,
        "tags": [],
    }


# ---------------------------------------------------------------------------
# Build plates / plate items (terv.md 28.)
# ---------------------------------------------------------------------------


def test_build_plate_and_items_roundtrip(project, version, owner):
    client = auth(owner)

    created = client.post(
        "/api/v1/build-plates/",
        {"project": project.pk, "name": "Plate A"},
        format="json",
    )
    assert created.status_code == 201
    plate_id = created.json()["id"]

    added = client.post(
        f"/api/v1/build-plates/{plate_id}/items/",
        {"model_version": version.pk, "position_x": 10.0},
        format="json",
    )
    assert added.status_code == 201
    assert added.json()["position_x"] == 10.0
    item_id = added.json()["id"]

    listed = client.get(f"/api/v1/build-plates/{plate_id}/items/")
    assert [row["id"] for row in listed.json()] == [item_id]

    removed = client.delete(
        f"/api/v1/build-plates/{plate_id}/items/", {"item_id": item_id}, format="json"
    )
    assert removed.status_code == 204
    assert client.get(f"/api/v1/build-plates/{plate_id}/items/").json() == []


def test_build_plate_is_workspace_scoped(project, owner, outsider):
    plate = BuildPlate.objects.create(project=project, name="Secret")
    assert auth(outsider).get("/api/v1/build-plates/").json()["results"] == []
    assert auth(outsider).get(f"/api/v1/build-plates/{plate.pk}/").status_code == 404


# ---------------------------------------------------------------------------
# Print job targets: model version XOR build plate
# ---------------------------------------------------------------------------


def test_print_job_accepts_a_build_plate(project, version, owner):
    plate = BuildPlate.objects.create(project=project, name="Plate")
    printer = Printer.objects.create(name="K2 Pro")
    client = auth(owner)

    response = client.post(
        "/api/v1/print-jobs/",
        {"project": project.pk, "build_plate": plate.pk, "printer": printer.pk},
        format="json",
    )
    assert response.status_code == 201
    assert response.json()["build_plate"] == plate.pk
    assert response.json()["version"] is None


def test_print_job_requires_exactly_one_target(project, version, owner):
    plate = BuildPlate.objects.create(project=project, name="Plate")
    printer = Printer.objects.create(name="K2 Pro")
    client = auth(owner)

    none = client.post(
        "/api/v1/print-jobs/", {"project": project.pk, "printer": printer.pk}, format="json"
    )
    assert none.status_code == 400

    both = client.post(
        "/api/v1/print-jobs/",
        {
            "project": project.pk,
            "model_version": version.pk,
            "build_plate": plate.pk,
            "printer": printer.pk,
        },
        format="json",
    )
    assert both.status_code == 400


def test_search_public_projects_only_returns_public_rows(workspace, owner):
    from projects.models import Project

    public = create_project(workspace=workspace, name="Public", created_by=owner)
    create_project(workspace=workspace, name="Private", created_by=owner)
    Project.objects.filter(pk=public.pk).update(is_public=True)

    assert [p.pk for p in search_public_projects()] == [public.pk]
    assert [p.pk for p in search_public_projects(q="Public")] == [public.pk]
