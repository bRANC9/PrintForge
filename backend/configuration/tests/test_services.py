"""Tests for ``configuration.services`` resolution order and overrides."""

from __future__ import annotations

import pytest
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.core.cache import cache

from configuration.models import AppSettings
from configuration.services import (
    SETTING_NAMES,
    effective_settings,
    get_setting,
    get_settings,
    update_settings,
)
from configuration.services import test_ollama as probe_ollama

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user():
    return User.objects.create_user(
        username="admin",
        email="admin@example.com",
        password="pw",
        is_staff=True,
    )


def test_get_settings_is_a_singleton():
    assert get_settings().pk == 1
    assert get_settings().pk == 1
    assert AppSettings.objects.count() == 1


def test_default_fallback_when_no_settings_attr_or_env(monkeypatch):
    monkeypatch.delenv("OPENSCAD_MODE", raising=False)

    assert get_setting("openscad_mode") == "local"


def test_effective_settings_shape_and_sources(monkeypatch):
    monkeypatch.delenv("OPENSCAD_MODE", raising=False)

    payload = effective_settings()

    assert set(payload) == {"effective", "overrides", "sources"}
    assert set(payload["effective"]) == set(SETTING_NAMES)
    assert set(payload["sources"]) == set(SETTING_NAMES)
    assert payload["sources"]["openscad_mode"] == "default"
    assert payload["overrides"]["ollama_model"] is None


def test_update_settings_overrides_and_invalidates_cache():
    update_settings(ollama_model="qwen3-coder:7b")

    assert get_setting("ollama_model") == "qwen3-coder:7b"
    payload = effective_settings()
    assert payload["effective"]["ollama_model"] == "qwen3-coder:7b"
    assert payload["overrides"]["ollama_model"] == "qwen3-coder:7b"
    assert payload["sources"]["ollama_model"] == "db"


def test_ollama_vision_model_is_registered_with_empty_default(monkeypatch):
    monkeypatch.delattr(django_settings, "OLLAMA_VISION_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_VISION_MODEL", raising=False)

    assert "ollama_vision_model" in SETTING_NAMES
    assert get_setting("ollama_vision_model") == ""
    assert effective_settings()["sources"]["ollama_vision_model"] == "default"


def test_ollama_vision_model_falls_back_to_settings_attr(monkeypatch):
    monkeypatch.setattr(django_settings, "OLLAMA_VISION_MODEL", "llava:13b")

    assert get_setting("ollama_vision_model") == "llava:13b"
    assert effective_settings()["sources"]["ollama_vision_model"] == "env"


def test_ollama_vision_model_db_override_round_trip():
    update_settings(ollama_vision_model="llava:7b")

    assert get_setting("ollama_vision_model") == "llava:7b"
    payload = effective_settings()
    assert payload["effective"]["ollama_vision_model"] == "llava:7b"
    assert payload["overrides"]["ollama_vision_model"] == "llava:7b"
    assert payload["sources"]["ollama_vision_model"] == "db"


def test_ollama_vision_model_empty_string_clears_override():
    update_settings(ollama_vision_model="llava:7b")
    assert effective_settings()["sources"]["ollama_vision_model"] == "db"

    update_settings(ollama_vision_model="")

    payload = effective_settings()
    assert payload["overrides"]["ollama_vision_model"] is None
    assert payload["sources"]["ollama_vision_model"] != "db"


def test_ollama_vision_model_max_length_is_enforced():
    with pytest.raises(ValueError, match="ollama_vision_model"):
        update_settings(ollama_vision_model="x" * 201)

    assert effective_settings()["overrides"]["ollama_vision_model"] is None


def test_rag_enabled_is_tri_state():
    update_settings(rag_enabled=False)

    assert get_setting("rag_enabled") is False
    assert effective_settings()["sources"]["rag_enabled"] == "db"
    assert effective_settings()["overrides"]["rag_enabled"] is False


def test_int_coercion_and_reset_to_fallback():
    update_settings(openscad_timeout_sec="120")
    assert get_setting("openscad_timeout_sec") == 120

    update_settings(openscad_timeout_sec=None)
    assert get_setting("openscad_timeout_sec") == 60
    assert effective_settings()["sources"]["openscad_timeout_sec"] != "db"


def test_unknown_setting_raises():
    with pytest.raises(ValueError, match="Unknown setting"):
        update_settings(nope=1)
    with pytest.raises(ValueError, match="Unknown setting"):
        get_setting("nope")


def test_bad_int_is_rejected():
    with pytest.raises(ValueError, match="integer"):
        update_settings(openscad_timeout_sec="abc")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("openscad_mode", "bogus"),
        ("slicer_mode", "bogus"),
        ("storage_backend", "s3"),
    ],
)
def test_invalid_choice_is_rejected_and_not_persisted(name, value):
    with pytest.raises(ValueError, match=name):
        update_settings(**{name: value})

    assert effective_settings()["overrides"][name] is None


def test_valid_choices_are_accepted():
    update_settings(openscad_mode="docker", slicer_mode="docker", storage_backend="local")

    assert get_setting("openscad_mode") == "docker"
    assert get_setting("slicer_mode") == "docker"


def test_field_validators_are_enforced():
    with pytest.raises(ValueError, match="openscad_timeout_sec"):
        update_settings(openscad_timeout_sec=-1)
    with pytest.raises(ValueError, match="ollama_model"):
        update_settings(ollama_model="x" * 201)


def test_docker_style_ollama_url_is_accepted():
    # Django's URLValidator rejects single-label hosts; Docker service names
    # must keep working (terv.md 18.3).
    update_settings(ollama_base_url="http://ollama:11434")

    assert get_setting("ollama_base_url") == "http://ollama:11434"


def test_empty_string_clears_override_for_every_type():
    update_settings(ollama_model="custom", openscad_timeout_sec=99, rag_enabled=True)
    assert get_setting("ollama_model") == "custom"
    assert get_setting("openscad_timeout_sec") == 99
    assert get_setting("rag_enabled") is True

    update_settings(ollama_model="", openscad_timeout_sec="", rag_enabled="")

    payload = effective_settings()
    assert payload["overrides"]["ollama_model"] is None
    assert payload["overrides"]["openscad_timeout_sec"] is None
    assert payload["overrides"]["rag_enabled"] is None
    assert payload["sources"]["ollama_model"] != "db"


def test_update_settings_records_user(user):
    update_settings(user=user, ollama_model="x")

    assert get_settings().updated_by_id == user.pk


def test_test_ollama_success(monkeypatch):
    class FakeResponse:
        status = 200

        def read(self, size=-1):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        "configuration.services.urllib.request.urlopen",
        lambda url, timeout: FakeResponse(),
    )

    result = probe_ollama()

    assert result["ok"] is True
    assert "reachable" in result["detail"]


def test_test_ollama_never_raises(monkeypatch):
    def boom(url, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr("configuration.services.urllib.request.urlopen", boom)

    result = probe_ollama()

    assert result["ok"] is False
    assert "connection refused" in result["detail"]
