"""Tests for the staff-only settings API and the ``configuration:settings`` page."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from rest_framework.test import APIClient

from api.views import MASKED_SETTING_VALUE, _is_secret_setting
from configuration.services import get_setting

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    cache.clear()
    yield
    cache.clear()


def auth(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def staff():
    return User.objects.create_user(
        username="staff",
        email="staff@example.com",
        password="pw",
        is_staff=True,
    )


@pytest.fixture
def member():
    return User.objects.create_user(
        username="member",
        email="member@example.com",
        password="pw",
    )


def test_settings_api_requires_authentication():
    assert APIClient().get("/api/v1/settings/").status_code in (401, 403)


def test_settings_api_requires_staff(member):
    client = auth(member)

    assert client.get("/api/v1/settings/").status_code == 403
    assert (
        client.patch("/api/v1/settings/", {"ollama_model": "x"}, format="json").status_code == 403
    )
    assert client.post("/api/v1/settings/test-ollama/").status_code == 403


def test_get_settings_payload_shape(staff):
    response = auth(staff).get("/api/v1/settings/")

    assert response.status_code == 200
    body = response.json()
    assert {"effective", "overrides", "sources", "read_only", "updated_at"} <= set(body)
    assert body["read_only"] == ["embedding_dim"]
    assert "embedding_dim" in body["effective"]
    assert body["updated_at"] is not None


def test_patch_settings_persists_and_returns_payload(staff):
    response = auth(staff).patch(
        "/api/v1/settings/",
        {"ollama_model": "qwen3-coder:7b"},
        format="json",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["effective"]["ollama_model"] == "qwen3-coder:7b"
    assert body["sources"]["ollama_model"] == "db"
    assert get_setting("ollama_model") == "qwen3-coder:7b"


def test_patch_settings_round_trips_vision_model(staff):
    response = auth(staff).patch(
        "/api/v1/settings/",
        {"ollama_vision_model": "llava:7b"},
        format="json",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["effective"]["ollama_vision_model"] == "llava:7b"
    assert body["overrides"]["ollama_vision_model"] == "llava:7b"
    assert body["sources"]["ollama_vision_model"] == "db"
    assert get_setting("ollama_vision_model") == "llava:7b"

    cleared = auth(staff).patch(
        "/api/v1/settings/",
        {"ollama_vision_model": ""},
        format="json",
    )
    assert cleared.status_code == 200
    assert cleared.json()["overrides"]["ollama_vision_model"] is None


def test_patch_settings_rejects_oversized_vision_model(staff):
    response = auth(staff).patch(
        "/api/v1/settings/",
        {"ollama_vision_model": "x" * 201},
        format="json",
    )

    assert response.status_code == 400
    assert "ollama_vision_model" in response.json()["detail"]


def test_patch_settings_round_trips_openai_model(staff):
    response = auth(staff).patch(
        "/api/v1/settings/",
        {"openai_model": "gpt-4o"},
        format="json",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["effective"]["openai_model"] == "gpt-4o"
    assert body["overrides"]["openai_model"] == "gpt-4o"
    assert body["sources"]["openai_model"] == "db"
    assert get_setting("openai_model") == "gpt-4o"


# ---------------------------------------------------------------------------
# Secret masking wiring (registry marker + name-based fallback)
# ---------------------------------------------------------------------------


def test_secret_classification_consults_registry_then_name_heuristic():
    assert _is_secret_setting("openai_api_key") is True  # registry secret marker
    assert _is_secret_setting("some_future_token") is True  # fallback heuristic
    assert _is_secret_setting("ollama_model") is False


def test_settings_api_masks_the_registry_secret(staff):
    response = auth(staff).patch(
        "/api/v1/settings/",
        {"openai_api_key": "sk-real"},
        format="json",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["effective"]["openai_api_key"] == MASKED_SETTING_VALUE
    assert body["overrides"]["openai_api_key"] == MASKED_SETTING_VALUE
    # The real value is still resolvable server-side.
    assert get_setting("openai_api_key") == "sk-real"


def test_settings_patch_mask_sentinel_keeps_the_stored_secret(staff):
    auth(staff).patch("/api/v1/settings/", {"openai_api_key": "sk-real"}, format="json")

    response = auth(staff).patch(
        "/api/v1/settings/",
        {"openai_api_key": MASKED_SETTING_VALUE},
        format="json",
    )

    assert response.status_code == 200
    assert get_setting("openai_api_key") == "sk-real"


def test_patch_rejects_unknown_name(staff):
    response = auth(staff).patch("/api/v1/settings/", {"nope": 1}, format="json")

    assert response.status_code == 400
    assert "nope" in response.json()


def test_patch_rejects_bad_type(staff):
    response = auth(staff).patch(
        "/api/v1/settings/",
        {"openscad_timeout_sec": "abc"},
        format="json",
    )

    assert response.status_code == 400


def test_patch_rejects_invalid_choice_and_names_the_field(staff):
    response = auth(staff).patch(
        "/api/v1/settings/",
        {"openscad_mode": "bogus"},
        format="json",
    )

    assert response.status_code == 400
    assert "openscad_mode" in response.json()["detail"]


def test_test_ollama_endpoint_delegates(staff, monkeypatch):
    monkeypatch.setattr("api.views.test_ollama", lambda: {"ok": True, "detail": "ok"})

    response = auth(staff).post("/api/v1/settings/test-ollama/")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "detail": "ok"}


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------


def test_settings_page_reverse():
    assert reverse("configuration:settings") == "/settings/"


def test_settings_page_requires_login():
    response = Client().get("/settings/")

    assert response.status_code == 302
    assert "/login/" in response["Location"]


def test_settings_page_forbidden_for_non_staff(member):
    client = Client()
    client.force_login(member)

    assert client.get("/settings/").status_code == 403


def test_settings_page_renders_for_staff(staff):
    client = Client()
    client.force_login(staff)

    assert client.get("/settings/").status_code == 200
