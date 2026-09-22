"""Tests for the staff-only Ollama management API and page route."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from rest_framework.test import APIClient

from configuration.models import OllamaPull, OllamaPullStatus
from configuration.services import OllamaError, RemoteCatalogError, get_setting

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


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


def test_ollama_api_requires_authentication():
    assert APIClient().get("/api/v1/ollama/models/").status_code in (401, 403)


def test_ollama_api_requires_staff(member):
    client = auth(member)

    assert client.get("/api/v1/ollama/models/").status_code == 403
    assert (
        client.post("/api/v1/ollama/models/pull/", {"name": "a"}, format="json").status_code == 403
    )
    assert client.delete("/api/v1/ollama/models/?name=a").status_code == 403
    assert (
        client.post("/api/v1/ollama/models/use/", {"name": "a"}, format="json").status_code == 403
    )
    assert client.get("/api/v1/ollama/pulls/").status_code == 403
    assert client.get("/api/v1/ollama/pulls/1/").status_code == 403


# ---------------------------------------------------------------------------
# GET /models/
# ---------------------------------------------------------------------------


def test_list_models_success(staff, monkeypatch):
    monkeypatch.setattr("api.views.ollama_version", lambda: {"version": "0.3.0"})
    monkeypatch.setattr("api.views.list_ollama_models", lambda: [{"name": "llama3:8b"}])

    response = auth(staff).get("/api/v1/ollama/models/")

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "0.3.0"
    assert body["models"] == [{"name": "llama3:8b"}]
    assert body["error"] is None
    assert "base_url" in body


def test_list_models_degrades_on_connection_error(staff, monkeypatch):
    monkeypatch.setattr("api.views.ollama_version", lambda: {"error": "connection refused"})

    response = auth(staff).get("/api/v1/ollama/models/")

    assert response.status_code == 200
    body = response.json()
    assert body["models"] == []
    assert body["error"] == "connection refused"
    assert body["version"] is None


# ---------------------------------------------------------------------------
# POST /models/pull/
# ---------------------------------------------------------------------------


def test_pull_enqueues_and_returns_the_pull(staff, monkeypatch):
    queued: list[int] = []
    monkeypatch.setattr("configuration.services.pull_ollama_model_task.delay", queued.append)

    response = auth(staff).post("/api/v1/ollama/models/pull/", {"name": "llama3:8b"}, format="json")

    assert response.status_code == 202
    body = response.json()
    assert body["name"] == "llama3:8b"
    assert body["status"] == OllamaPullStatus.PENDING
    assert queued == [body["id"]]


@pytest.mark.parametrize("name", ["", "bad name", "a" * 201])
def test_pull_rejects_invalid_names(staff, name):
    response = auth(staff).post("/api/v1/ollama/models/pull/", {"name": name}, format="json")

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /models/?name=
# ---------------------------------------------------------------------------


def test_delete_model(staff, monkeypatch):
    called: list[str] = []
    monkeypatch.setattr("api.views.delete_ollama_model", lambda *, name: called.append(name))

    response = auth(staff).delete("/api/v1/ollama/models/?name=llama3:8b")

    assert response.status_code == 204
    assert called == ["llama3:8b"]


def test_delete_model_requires_a_name(staff):
    response = auth(staff).delete("/api/v1/ollama/models/")

    assert response.status_code == 400


def test_delete_model_reports_ollama_failure(staff, monkeypatch):
    def boom(*, name):
        raise OllamaError("Ollama returned HTTP 404: model not found")

    monkeypatch.setattr("api.views.delete_ollama_model", boom)

    response = auth(staff).delete("/api/v1/ollama/models/?name=missing:1b")

    assert response.status_code == 502
    assert "404" in response.json()["detail"]


# ---------------------------------------------------------------------------
# POST /models/use/
# ---------------------------------------------------------------------------


def test_use_model_sets_the_runtime_setting(staff):
    response = auth(staff).post("/api/v1/ollama/models/use/", {"name": "llama3:8b"}, format="json")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "ollama_model": "llama3:8b"}
    assert get_setting("ollama_model") == "llama3:8b"


def test_use_model_rejects_an_invalid_name(staff):
    response = auth(staff).post("/api/v1/ollama/models/use/", {"name": ""}, format="json")

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# GET /pulls/
# ---------------------------------------------------------------------------


def test_pulls_list_and_detail(staff):
    first = OllamaPull.objects.create(name="a:1b")
    second = OllamaPull.objects.create(name="b:1b")
    client = auth(staff)

    listing = client.get("/api/v1/ollama/pulls/").json()
    assert [item["id"] for item in listing] == [second.pk, first.pk]

    detail = client.get(f"/api/v1/ollama/pulls/{first.pk}/")
    assert detail.status_code == 200
    assert detail.json()["name"] == "a:1b"

    assert client.get("/api/v1/ollama/pulls/999999/").status_code == 404


# ---------------------------------------------------------------------------
# GET /recommendations/  and  GET /remote/
# ---------------------------------------------------------------------------


def test_advisor_endpoints_require_staff(member):
    client = auth(member)

    assert client.get("/api/v1/ollama/recommendations/?vram_gb=8").status_code == 403
    assert client.get("/api/v1/ollama/remote/?source=ollama").status_code == 403


def test_recommendations_returns_the_service_payload(staff, monkeypatch):
    captured = {}

    def fake(**kwargs):
        captured.update(kwargs)
        return {"vram_gb": 8.0, "context": 8192, "recommendations": [], "tiers": []}

    monkeypatch.setattr("api.views.model_recommendations", fake)

    response = auth(staff).get("/api/v1/ollama/recommendations/?vram_gb=8&context=16384")

    assert response.status_code == 200
    assert captured["vram_gb"] == 8.0
    assert captured["context"] == 16384
    assert response.json()["vram_gb"] == 8.0


def test_recommendations_requires_a_positive_vram(staff):
    assert auth(staff).get("/api/v1/ollama/recommendations/").status_code == 400
    assert auth(staff).get("/api/v1/ollama/recommendations/?vram_gb=0").status_code == 400


def test_remote_ollama_source(staff, monkeypatch):
    monkeypatch.setattr(
        "api.views.list_ollama_library_models",
        lambda *, limit: [{"name": "glm-5", "pull_name": "glm-5", "source": "ollama"}],
    )

    response = auth(staff).get("/api/v1/ollama/remote/?source=ollama&limit=5")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "ollama"
    assert body["error"] is None
    assert body["models"][0]["name"] == "glm-5"


def test_remote_huggingface_source(staff, monkeypatch):
    captured = {}

    def fake(query, *, limit):
        captured.update({"query": query, "limit": limit})
        return [{"name": "unsloth/x-GGUF", "pull_name": "hf.co/unsloth/x-GGUF:Q4_K_M"}]

    monkeypatch.setattr("api.views.search_huggingface_models", fake)

    response = auth(staff).get("/api/v1/ollama/remote/?source=huggingface&q=qwen&limit=30")

    assert response.status_code == 200
    assert captured == {"query": "qwen", "limit": 10}
    assert response.json()["models"][0]["pull_name"].startswith("hf.co/")


def test_remote_degrades_on_catalog_error(staff, monkeypatch):
    def boom(*args, **kwargs):
        raise RemoteCatalogError("connection refused")

    monkeypatch.setattr("api.views.list_ollama_library_models", boom)

    response = auth(staff).get("/api/v1/ollama/remote/?source=ollama")

    assert response.status_code == 200
    body = response.json()
    assert body["models"] == []
    assert "connection refused" in body["error"]


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------


def test_ollama_page_reverse():
    assert reverse("configuration:ollama") == "/settings/ollama/"


def test_ollama_page_requires_login():
    response = Client().get("/settings/ollama/")

    assert response.status_code == 302
    assert "/login/" in response["Location"]


def test_ollama_page_forbidden_for_non_staff(member):
    client = Client()
    client.force_login(member)

    assert client.get("/settings/ollama/").status_code == 403
