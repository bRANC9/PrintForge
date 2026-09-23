"""The settings API must never echo secret values back in cleartext.

``configuration.models.AppSettings`` stores ``openai_api_key`` as a plain
``CharField`` (no dedicated secret field), so ``api.views`` owns the masking
(docs/planner-clarification.md is unrelated; this is the API hygiene fix from
the same review). PATCH must still be able to set the real value.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from api.views import MASKED_SETTING_VALUE
from configuration.services import get_setting

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """The settings singleton is cached process-wide; never leak between tests."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _empty_openai_key():
    """Make the env/default resolution deterministic (no real key in tests)."""
    with override_settings(OPENAI_API_KEY=""):
        yield


def _staff_client():
    user = User.objects.create_user(username="admin", password="pw", is_staff=True)
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_settings_get_masks_the_openai_key():
    client = _staff_client()
    client.patch("/api/v1/settings/", {"openai_api_key": "sk-real"}, format="json")

    body = client.get("/api/v1/settings/").json()

    assert body["effective"]["openai_api_key"] == MASKED_SETTING_VALUE
    assert body["overrides"]["openai_api_key"] == MASKED_SETTING_VALUE
    # The real value is still resolvable server-side.
    assert get_setting("openai_api_key") == "sk-real"


def test_settings_patch_with_a_real_value_still_persists():
    client = _staff_client()

    response = client.patch("/api/v1/settings/", {"openai_api_key": "sk-new"}, format="json")

    assert response.status_code == 200
    assert response.json()["effective"]["openai_api_key"] == MASKED_SETTING_VALUE
    assert get_setting("openai_api_key") == "sk-new"


def test_settings_patch_with_the_mask_sentinel_does_not_overwrite():
    client = _staff_client()
    client.patch("/api/v1/settings/", {"openai_api_key": "sk-real"}, format="json")

    response = client.patch(
        "/api/v1/settings/", {"openai_api_key": MASKED_SETTING_VALUE}, format="json"
    )

    assert response.status_code == 200
    assert get_setting("openai_api_key") == "sk-real"


def test_settings_patch_can_still_clear_the_key():
    client = _staff_client()
    client.patch("/api/v1/settings/", {"openai_api_key": "sk-real"}, format="json")

    client.patch("/api/v1/settings/", {"openai_api_key": ""}, format="json")

    assert get_setting("openai_api_key") == ""


def test_unset_secret_stays_empty_not_masked():
    body = _staff_client().get("/api/v1/settings/").json()

    assert body["effective"]["openai_api_key"] == ""
    assert body["overrides"]["openai_api_key"] is None


def test_non_secret_settings_are_not_masked():
    client = _staff_client()
    client.patch("/api/v1/settings/", {"ollama_model": "qwen3-coder:7b"}, format="json")

    body = client.get("/api/v1/settings/").json()

    assert body["effective"]["ollama_model"] == "qwen3-coder:7b"
    assert body["overrides"]["ollama_model"] == "qwen3-coder:7b"
