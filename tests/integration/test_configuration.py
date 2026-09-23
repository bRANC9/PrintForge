"""Runtime settings + configuration integration tests.

Covers ``configuration.services`` resolution order (DB override -> Django
settings -> ``os.environ`` -> default), the staff-only settings API/page, the
30 s cache invalidation, and that consumers actually pick up a runtime override
without a restart (terv.md 19. fejezet).
"""

from __future__ import annotations

import pytest
from django.conf import settings as django_settings
from django.core.cache import cache
from django.test import Client
from factories import UserFactory
from rest_framework.test import APIClient

from agents.llm import OllamaProvider
from configuration.models import AppSettings
from configuration.services import (
    SETTING_NAMES,
    effective_settings,
    get_setting,
    get_settings,
    invalidate_settings_cache,
    update_settings,
)
from configuration.services import test_ollama as probe_ollama
from designs.cad.openscad import OpenSCADBackend
from files.services import LocalStorage, get_storage
from slicers.prusaslicer import PrusaSlicerBackend, PrusaSlicerConfig

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """The singleton is cached process-wide; never let it leak between tests."""
    cache.clear()
    yield
    cache.clear()


def auth(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


# ---------------------------------------------------------------------------
# Resolution order
# ---------------------------------------------------------------------------


def test_db_override_wins_over_settings_and_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "from-env")
    monkeypatch.setattr(django_settings, "OLLAMA_MODEL", "from-settings")

    update_settings(ollama_model="from-db")

    assert get_setting("ollama_model") == "from-db"
    payload = effective_settings()
    assert payload["sources"]["ollama_model"] == "db"
    assert payload["overrides"]["ollama_model"] == "from-db"


def test_db_override_wins_over_env(monkeypatch):
    monkeypatch.delenv("OPENSCAD_MODE", raising=False)
    monkeypatch.setenv("OPENSCAD_MODE", "docker")

    update_settings(openscad_mode="local")

    assert get_setting("openscad_mode") == "local"
    assert effective_settings()["sources"]["openscad_mode"] == "db"


def test_clearing_override_falls_back_to_django_settings(monkeypatch):
    monkeypatch.setattr(django_settings, "OPENSCAD_TIMEOUT_SEC", 77)
    update_settings(openscad_timeout_sec=120)
    assert get_setting("openscad_timeout_sec") == 120

    update_settings(openscad_timeout_sec=None)

    assert get_setting("openscad_timeout_sec") == 77
    assert effective_settings()["sources"]["openscad_timeout_sec"] == "env"


def test_clearing_override_falls_back_to_env_then_default(monkeypatch):
    monkeypatch.delenv("OPENSCAD_MODE", raising=False)
    monkeypatch.setenv("OPENSCAD_MODE", "docker")

    update_settings(openscad_mode="local")
    assert get_setting("openscad_mode") == "local"

    update_settings(openscad_mode=None)
    assert get_setting("openscad_mode") == "docker"
    assert effective_settings()["sources"]["openscad_mode"] == "env"

    monkeypatch.delenv("OPENSCAD_MODE")
    assert get_setting("openscad_mode") == "local"
    assert effective_settings()["sources"]["openscad_mode"] == "default"


def test_env_is_used_when_no_db_or_django_setting(monkeypatch):
    monkeypatch.delenv("OPENSCAD_MODE", raising=False)
    assert not hasattr(django_settings, "OPENSCAD_MODE")
    monkeypatch.setenv("OPENSCAD_MODE", "docker")

    assert get_setting("openscad_mode") == "docker"
    assert effective_settings()["sources"]["openscad_mode"] == "env"


def test_empty_string_clears_a_string_override(monkeypatch):
    monkeypatch.setattr(django_settings, "OLLAMA_MODEL", "settings-model")
    update_settings(ollama_model="db-model")

    update_settings(ollama_model="")

    assert get_setting("ollama_model") == "settings-model"
    assert effective_settings()["sources"]["ollama_model"] == "env"
    assert effective_settings()["overrides"]["ollama_model"] is None


# ---------------------------------------------------------------------------
# rag_enabled tri-state
# ---------------------------------------------------------------------------


def test_rag_enabled_false_override_beats_true_setting(monkeypatch):
    monkeypatch.setattr(django_settings, "RAG_ENABLED", True)

    update_settings(rag_enabled=False)

    assert get_setting("rag_enabled") is False
    payload = effective_settings()
    assert payload["sources"]["rag_enabled"] == "db"
    assert payload["overrides"]["rag_enabled"] is False

    update_settings(rag_enabled=None)

    assert get_setting("rag_enabled") is True
    assert effective_settings()["sources"]["rag_enabled"] == "env"


def test_rag_enabled_true_override_beats_false_setting(monkeypatch):
    monkeypatch.setattr(django_settings, "RAG_ENABLED", False)

    update_settings(rag_enabled=True)

    assert get_setting("rag_enabled") is True
    assert effective_settings()["sources"]["rag_enabled"] == "db"


def test_rag_enabled_accepts_truthy_strings():
    update_settings(rag_enabled="true")
    assert get_setting("rag_enabled") is True

    update_settings(rag_enabled="off")
    assert get_setting("rag_enabled") is False


# ---------------------------------------------------------------------------
# Int coercion / validation
# ---------------------------------------------------------------------------


def test_int_override_is_coerced_and_reset(monkeypatch):
    monkeypatch.setattr(django_settings, "OPENSCAD_TIMEOUT_SEC", 60)

    update_settings(openscad_timeout_sec="120")

    value = get_setting("openscad_timeout_sec")
    assert value == 120
    assert isinstance(value, int)

    update_settings(openscad_timeout_sec=None)

    assert get_setting("openscad_timeout_sec") == 60
    assert effective_settings()["sources"]["openscad_timeout_sec"] == "env"


def test_non_integer_value_is_rejected():
    with pytest.raises(ValueError, match="integer"):
        update_settings(openscad_timeout_sec="abc")


def test_unknown_setting_names_are_rejected():
    with pytest.raises(ValueError, match="Unknown setting"):
        get_setting("does_not_exist")

    with pytest.raises(ValueError, match="Unknown setting"):
        update_settings(does_not_exist=1)


# ---------------------------------------------------------------------------
# effective_settings() shape / sources
# ---------------------------------------------------------------------------


def test_effective_settings_shape_and_sources(monkeypatch):
    monkeypatch.delenv("OPENSCAD_MODE", raising=False)

    payload = effective_settings()

    assert set(payload) == {"effective", "overrides", "sources"}
    assert set(payload["effective"]) == set(SETTING_NAMES)
    assert set(payload["overrides"]) == set(SETTING_NAMES)
    assert set(payload["sources"]) == set(SETTING_NAMES)

    # No DB row, no settings attribute, no env -> default.
    assert payload["sources"]["openscad_mode"] == "default"
    # config/settings.py defines these, so they resolve from the settings layer.
    assert payload["sources"]["ollama_model"] == "env"
    assert payload["sources"]["storage_backend"] == "env"
    assert payload["overrides"]["ollama_model"] is None

    update_settings(ollama_model="runtime-model")

    payload = effective_settings()
    assert payload["sources"]["ollama_model"] == "db"
    assert payload["overrides"]["ollama_model"] == "runtime-model"
    assert payload["effective"]["ollama_model"] == "runtime-model"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_update_settings_invalidates_the_cache_immediately():
    update_settings(ollama_model="first")
    assert get_setting("ollama_model") == "first"

    update_settings(ollama_model="second")

    assert get_setting("ollama_model") == "second"
    assert effective_settings()["effective"]["ollama_model"] == "second"


def test_settings_singleton_is_cached_and_can_be_invalidated(monkeypatch):
    loads: list[int] = []
    original_load = AppSettings.load

    def counting_load():
        loads.append(1)
        return original_load()

    monkeypatch.setattr(AppSettings, "load", staticmethod(counting_load))

    get_settings()
    get_settings()
    assert len(loads) == 1  # second read served from the cache

    invalidate_settings_cache()
    get_settings()
    assert len(loads) == 2  # invalidated -> reloaded

    assert AppSettings.objects.count() == 1


def test_rejected_update_does_not_leak_into_the_cached_singleton():
    """A rejected PATCH must not partially mutate the cached settings object.

    ``update_settings`` mutates the cached singleton before it validates/saves
    every field; when one field is invalid the exception skips the cache
    invalidation, so a naive implementation leaves the earlier field changed in
    memory even though the 400 means "nothing was persisted".
    """
    baseline = get_setting("ollama_model")
    client = auth(UserFactory(is_staff=True))

    response = client.patch(
        "/api/v1/settings/",
        {"ollama_model": "leaked", "openscad_timeout_sec": "not-an-int"},
        format="json",
    )

    assert response.status_code == 400
    assert get_setting("ollama_model") == baseline


# ---------------------------------------------------------------------------
# test_ollama() service (no network)
# ---------------------------------------------------------------------------


def test_test_ollama_reports_reachable(monkeypatch):
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


# ---------------------------------------------------------------------------
# API permissions
# ---------------------------------------------------------------------------


def test_settings_api_requires_authentication():
    client = APIClient()

    assert client.get("/api/v1/settings/").status_code in (401, 403)
    assert client.patch("/api/v1/settings/", {"ollama_model": "x"}, format="json").status_code in (
        401,
        403,
    )
    assert client.post("/api/v1/settings/test-ollama/").status_code in (401, 403)


def test_settings_api_forbidden_for_non_staff():
    client = auth(UserFactory())

    assert client.get("/api/v1/settings/").status_code == 403
    assert (
        client.patch("/api/v1/settings/", {"ollama_model": "x"}, format="json").status_code == 403
    )
    assert client.post("/api/v1/settings/test-ollama/").status_code == 403


def test_settings_api_allows_staff_and_persists():
    staff = UserFactory(is_staff=True)
    client = auth(staff)

    response = client.get("/api/v1/settings/")
    assert response.status_code == 200
    body = response.json()
    assert {"effective", "overrides", "sources", "read_only", "updated_at"} <= set(body)
    assert body["read_only"] == ["embedding_dim"]
    assert "embedding_dim" in body["effective"]

    patched = client.patch("/api/v1/settings/", {"ollama_model": "qwen3-coder:7b"}, format="json")
    assert patched.status_code == 200
    assert patched.json()["effective"]["ollama_model"] == "qwen3-coder:7b"
    assert patched.json()["sources"]["ollama_model"] == "db"

    assert get_setting("ollama_model") == "qwen3-coder:7b"
    assert get_settings().updated_by_id == staff.id


def test_settings_api_rejects_unknown_name():
    response = auth(UserFactory(is_staff=True)).patch(
        "/api/v1/settings/", {"nope": 1}, format="json"
    )

    assert response.status_code == 400
    assert "nope" in response.json()


def test_settings_api_rejects_bad_type():
    response = auth(UserFactory(is_staff=True)).patch(
        "/api/v1/settings/", {"openscad_timeout_sec": "abc"}, format="json"
    )

    assert response.status_code == 400


def test_settings_api_rejects_an_invalid_mode_choice():
    """Regression: the API validates the model's declared enum choices."""
    response = auth(UserFactory(is_staff=True)).patch(
        "/api/v1/settings/", {"openscad_mode": "bogus"}, format="json"
    )

    assert response.status_code == 400


def test_test_ollama_endpoint_delegates(monkeypatch):
    monkeypatch.setattr("api.views.test_ollama", lambda: {"ok": True, "detail": "ok"})

    response = auth(UserFactory(is_staff=True)).post("/api/v1/settings/test-ollama/")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "detail": "ok"}


# ---------------------------------------------------------------------------
# Page permissions
# ---------------------------------------------------------------------------


def test_settings_page_requires_login():
    response = Client().get("/settings/")

    assert response.status_code == 302
    assert "/login/" in response["Location"]


def test_settings_page_forbidden_for_non_staff():
    client = Client()
    client.force_login(UserFactory())

    assert client.get("/settings/").status_code == 403


def test_settings_page_renders_for_staff():
    client = Client()
    client.force_login(UserFactory(is_staff=True))

    assert client.get("/settings/").status_code == 200


# ---------------------------------------------------------------------------
# Consumer wiring: a runtime override changes behaviour without a restart
# ---------------------------------------------------------------------------


def test_ollama_provider_picks_up_runtime_override():
    update_settings(ollama_model="qwen3-coder:7b", ollama_base_url="http://runtime:11434/")

    provider = OllamaProvider()

    assert provider.model == "qwen3-coder:7b"
    assert provider.base_url == "http://runtime:11434"


def test_ollama_provider_explicit_args_win_over_runtime_override():
    update_settings(ollama_model="qwen3-coder:7b")

    provider = OllamaProvider(model="explicit-model")

    assert provider.model == "explicit-model"


def test_openscad_backend_picks_up_runtime_override():
    update_settings(
        openscad_mode="docker",
        openscad_timeout_sec=7,
        openscad_memory_limit="2g",
        openscad_cpu_limit="3.0",
    )

    config = OpenSCADBackend().config

    assert config.mode == "docker"
    assert config.timeout_sec == 7.0
    assert config.memory_limit == "2g"
    assert config.cpu_limit == "3.0"


def test_openscad_backend_explicit_timeout_wins():
    update_settings(openscad_timeout_sec=7)

    assert OpenSCADBackend(timeout_sec=99).config.timeout_sec == 99


def test_prusaslicer_backend_picks_up_runtime_override():
    update_settings(slicer_mode="docker", slicer_timeout_sec=42)

    config = PrusaSlicerBackend().resolve_config()

    assert config.mode == "docker"
    assert config.timeout_sec == 42.0


def test_prusaslicer_explicit_config_wins():
    update_settings(slicer_mode="docker", slicer_timeout_sec=42)
    explicit = PrusaSlicerConfig(mode="local", timeout_sec=1.0)

    config = PrusaSlicerBackend(config=explicit).resolve_config()

    assert config.mode == "local"
    assert config.timeout_sec == 1.0


def test_rag_enabled_consumer_reads_runtime_setting(settings):
    import embeddings.services as embedding_services

    settings.RAG_ENABLED = False
    update_settings(rag_enabled=True)
    assert embedding_services.rag_enabled() is True

    update_settings(rag_enabled=False)
    assert embedding_services.rag_enabled() is False


def test_embedding_model_consumer_reads_runtime_setting(settings):
    import embeddings.services as embedding_services

    settings.EMBEDDING_MODEL = "bge-m3"
    update_settings(embedding_model="nomic-embed-text")
    assert embedding_services._embedding_model() == "nomic-embed-text"

    update_settings(embedding_model=None)
    assert embedding_services._embedding_model() == "bge-m3"


# ---------------------------------------------------------------------------
# Storage: DB-backed resolution + DB-less fallback
# ---------------------------------------------------------------------------


def test_get_storage_honours_the_runtime_override(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.STORAGE_BACKEND = "local"
    update_settings(storage_backend="local")

    storage = get_storage()

    assert isinstance(storage, LocalStorage)
    assert storage.root == tmp_path

    # "gcs" is not an allowed DB override (field choices), so it is rejected...
    with pytest.raises(ValueError, match="storage_backend"):
        update_settings(storage_backend="gcs")

    # ...while "s3" is an allowed override and, like the static setting, selects
    # the S3/MinIO backend.
    update_settings(storage_backend="s3")
    from files.services import S3Storage

    assert isinstance(get_storage(), S3Storage)

    update_settings(storage_backend=None)
    settings.STORAGE_BACKEND = "s3"

    assert isinstance(get_storage(), S3Storage)

    # An unknown backend from the static settings still surfaces as
    # NotImplementedError.
    settings.STORAGE_BACKEND = "gcs"
    with pytest.raises(NotImplementedError, match="gcs"):
        get_storage()

    settings.STORAGE_BACKEND = "local"
    assert isinstance(get_storage(), LocalStorage)


def test_get_storage_falls_back_when_the_settings_lookup_fails(settings, monkeypatch):
    settings.STORAGE_BACKEND = "local"

    def boom(name):
        raise RuntimeError("settings database is down")

    monkeypatch.setattr("files.services.get_setting", boom)

    assert isinstance(get_storage(), LocalStorage)

    settings.STORAGE_BACKEND = "gcs"
    with pytest.raises(NotImplementedError, match="gcs"):
        get_storage()
