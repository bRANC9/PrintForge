"""Tests for the own-profile page and its ``/api/v1/me/`` endpoints.

Covers the two things that are easy to get wrong about a "me" endpoint: that it
can only ever see the caller, and that a password change keeps the session
alive (a naive ``set_password`` logs the user straight out).
"""

from __future__ import annotations

import pytest
from factories import (
    ModelDownloadFactory,
    ModelVersionFactory,
    PrintJobFactory,
    ProjectFactory,
    UserFactory,
    WorkspaceFactory,
)
from rest_framework.test import APIClient

from printers.models import PrintJobStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def auth_client(user):
    api = APIClient()
    api.force_authenticate(user=user)
    return api


@pytest.fixture
def user():
    return UserFactory(username="ada", display_name="Ada", email="ada@example.com")


# -- the page ---------------------------------------------------------------


def test_profile_page_requires_login(client):
    response = client.get("/profile/")

    assert response.status_code in (301, 302)
    assert "/login/" in response.url


def test_profile_page_renders_for_a_signed_in_user(user):
    from django.test import Client

    web = Client()
    web.force_login(user)

    response = web.get("/profile/")

    assert response.status_code == 200
    assert response.templates[0].name == "accounts/profile.html"


def test_header_links_the_username_to_the_profile(user):
    from django.test import Client

    web = Client()
    web.force_login(user)

    body = web.get("/").content.decode()

    assert 'href="/profile/"' in body


# -- GET /api/v1/me/ --------------------------------------------------------


def test_me_requires_authentication(client):
    assert client.get("/api/v1/me/").status_code in (401, 403)


def test_me_returns_the_caller_fields_and_stats(auth_client, user):
    response = auth_client.get("/api/v1/me/")

    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "ada"
    assert data["display_name"] == "Ada"
    assert data["email"] == "ada@example.com"
    assert data["is_staff"] is False
    assert set(data["stats"]) == {"workspaces", "content", "reach", "printing", "activity"}


def test_me_stats_aggregate_the_callers_work(auth_client, user):
    workspace = WorkspaceFactory(owner=user)
    project = ProjectFactory(workspace=workspace, created_by=user, download_count=4)
    version = ModelVersionFactory(created_by=user, project=project)
    # Pass the version explicitly: PrintJobFactory would otherwise create a
    # second one via its SubFactory, which would also count as our version.
    PrintJobFactory(
        created_by=user,
        project=project,
        model_version=version,
        status=PrintJobStatus.COMPLETED,
    )
    ModelDownloadFactory(project=project)

    stats = auth_client.get("/api/v1/me/").json()["stats"]

    assert stats["workspaces"]["owned"] == 1
    assert stats["content"]["projects"] == 1
    assert stats["content"]["versions"] == 1
    assert stats["printing"]["completed"] == 1
    assert stats["reach"]["downloads"] == 4


def test_me_never_exposes_another_users_data(auth_client, user):
    stranger = UserFactory(username="bob", display_name="Bob")
    stranger_project = ProjectFactory(created_by=stranger)
    PrintJobFactory(created_by=stranger, project=stranger_project)
    ModelDownloadFactory(project=stranger_project)

    data = auth_client.get("/api/v1/me/").json()

    assert data["username"] == "ada"
    assert data["display_name"] == "Ada"
    assert data["stats"]["content"]["projects"] == 0
    assert data["stats"]["printing"]["jobs"] == 0
    assert data["stats"]["reach"]["download_events"] == 0


def test_me_never_leaks_the_password_hash(auth_client):
    data = auth_client.get("/api/v1/me/").json()

    assert "password" not in data
    assert not any("password" in key for key in data)


# -- PATCH /api/v1/me/ ------------------------------------------------------


def test_me_patch_updates_the_editable_fields(auth_client, user):
    response = auth_client.patch(
        "/api/v1/me/",
        {"display_name": "Ada L.", "email": "ada.l@example.com", "last_name": "Lovelace"},
        format="json",
    )

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.display_name == "Ada L."
    assert user.email == "ada.l@example.com"
    assert user.last_name == "Lovelace"


def test_me_patch_returns_recomputed_stats(auth_client, user):
    ProjectFactory(created_by=user)

    data = auth_client.patch("/api/v1/me/", {"display_name": "X"}, format="json").json()

    assert data["display_name"] == "X"
    assert data["stats"]["content"]["projects"] == 1


def test_me_patch_ignores_username(auth_client, user):
    response = auth_client.patch("/api/v1/me/", {"username": "hijacked"}, format="json")

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.username == "ada"


def test_me_patch_ignores_privilege_escalation_attempts(auth_client, user):
    auth_client.patch(
        "/api/v1/me/",
        {"is_staff": True, "is_superuser": True, "id": 999},
        format="json",
    )

    user.refresh_from_db()
    assert user.is_staff is False
    assert user.is_superuser is False


def test_me_patch_rejects_a_duplicate_email(auth_client, user):
    UserFactory(email="taken@example.com")

    response = auth_client.patch("/api/v1/me/", {"email": "taken@example.com"}, format="json")

    assert response.status_code == 400
    assert "email" in response.json()
    user.refresh_from_db()
    assert user.email == "ada@example.com"


def test_me_patch_rejects_a_malformed_email(auth_client, user):
    response = auth_client.patch("/api/v1/me/", {"email": "not-an-email"}, format="json")

    assert response.status_code == 400
    user.refresh_from_db()
    assert user.email == "ada@example.com"


def test_me_patch_allows_saving_the_same_email_again(auth_client, user):
    """The unique validator must exclude the caller's own row."""
    response = auth_client.patch("/api/v1/me/", {"email": "ada@example.com"}, format="json")

    assert response.status_code == 200


def test_me_patch_requires_authentication(client):
    response = client.patch("/api/v1/me/", {"display_name": "X"}, format="json")

    assert response.status_code in (401, 403)


# -- POST /api/v1/me/password/ ---------------------------------------------


NEW_PASSWORD = "orbital-mechanics-42"


def _change_password(api, *, old, new1, new2=None):
    return api.post(
        "/api/v1/me/password/",
        {"old_password": old, "new_password1": new1, "new_password2": new2 or new1},
        format="json",
    )


def test_password_change_succeeds_and_really_changes_the_password(user):
    api = APIClient()
    api.force_authenticate(user=user)

    response = _change_password(api, old="test-pass-123", new1=NEW_PASSWORD, new2=NEW_PASSWORD)

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)
    assert not user.check_password("test-pass-123")


def test_password_change_rejects_a_wrong_current_password(user):
    api = APIClient()
    api.force_authenticate(user=user)

    response = _change_password(api, old="wrong-password", new1=NEW_PASSWORD)

    assert response.status_code == 400
    assert "old_password" in response.json()
    user.refresh_from_db()
    assert user.check_password("test-pass-123")


def test_password_change_rejects_a_mismatched_confirmation(user):
    api = APIClient()
    api.force_authenticate(user=user)

    response = _change_password(
        api, old="test-pass-123", new1=NEW_PASSWORD, new2="something-else-9"
    )

    assert response.status_code == 400
    assert "new_password2" in response.json()
    user.refresh_from_db()
    assert user.check_password("test-pass-123")


def test_password_change_runs_the_configured_validators(user):
    """``MinimumLengthValidator`` and friends are configured, so they must bite."""
    api = APIClient()
    api.force_authenticate(user=user)

    response = _change_password(api, old="test-pass-123", new1="123")

    assert response.status_code == 400
    user.refresh_from_db()
    assert user.check_password("test-pass-123")


def test_password_change_rejects_a_password_similar_to_a_user_attribute():
    """``UserAttributeSimilarityValidator`` must reject a near-copy."""
    similar = UserFactory(username="ada", first_name="Workshop")
    api = APIClient()
    api.force_authenticate(user=similar)

    response = _change_password(api, old="test-pass-123", new1="Workshop")

    assert response.status_code == 400
    # Assert on the stable machine-readable code, not the message: the message
    # is translated (the project runs with LANGUAGE_CODE="hu").
    assert "password_too_similar" in str(response.json())


def test_password_change_requires_authentication(client):
    response = _change_password(client, old="x", new1=NEW_PASSWORD)

    assert response.status_code in (401, 403)


def test_password_change_keeps_the_session_alive(user):
    """The caller must not be logged out by changing their own password."""
    from django.test import Client

    web = Client()
    web.force_login(user)

    response = web.post(
        "/api/v1/me/password/",
        data={
            "old_password": "test-pass-123",
            "new_password1": NEW_PASSWORD,
            "new_password2": NEW_PASSWORD,
        },
    )

    assert response.status_code == 200
    # Still authenticated: a protected page keeps working.
    assert web.get("/api/v1/me/").status_code == 200
    assert web.get("/profile/").status_code == 200
