"""Tests for the Ollama model-management service functions (HTTP stubbed)."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from configuration.models import OllamaPull, OllamaPullStatus
from configuration.services import (
    OllamaError,
    delete_ollama_model,
    list_ollama_models,
    list_pulls,
    ollama_version,
    pull_ollama_model,
    pull_status,
    show_ollama_model,
)

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def user():
    return User.objects.create_user(
        username="puller",
        email="puller@example.com",
        password="pw",
    )


def test_ollama_version_returns_version(monkeypatch):
    monkeypatch.setattr("configuration.services._ollama_json", lambda *a, **k: {"version": "0.3.0"})

    assert ollama_version() == {"version": "0.3.0"}


def test_ollama_version_returns_error_on_failure(monkeypatch):
    def boom(*a, **k):
        raise OllamaError("connection refused")

    monkeypatch.setattr("configuration.services._ollama_json", boom)

    assert ollama_version() == {"error": "connection refused"}


def test_list_ollama_models_maps_fields(monkeypatch):
    monkeypatch.setattr(
        "configuration.services._ollama_json",
        lambda *a, **k: {
            "models": [
                {
                    "name": "llama3:8b",
                    "size": 5_000_000_000,
                    "modified_at": "2026-09-21T10:00:00Z",
                    "digest": "abc123",
                    "details": {
                        "family": "llama",
                        "parameter_size": "8B",
                        "quantization_level": "Q4_0",
                    },
                }
            ]
        },
    )

    models = list_ollama_models()

    assert models == [
        {
            "name": "llama3:8b",
            "size": 5_000_000_000,
            "size_human": "4.7 GB",
            "modified_at": "2026-09-21T10:00:00Z",
            "digest": "abc123",
            "family": "llama",
            "parameter_size": "8B",
            "quantization": "Q4_0",
        }
    ]


def test_show_ollama_model_posts_the_name(monkeypatch):
    captured = {}

    def fake_json(url, *, method="GET", payload=None, timeout=10):
        captured.update({"url": url, "method": method, "payload": payload})
        return {"modelfile": "FROM llama3"}

    monkeypatch.setattr("configuration.services._ollama_json", fake_json)

    result = show_ollama_model("llama3:8b")

    assert result == {"modelfile": "FROM llama3"}
    assert captured["method"] == "POST"
    assert captured["payload"] == {"name": "llama3:8b"}
    assert captured["url"].endswith("/api/show")


def test_delete_ollama_model_calls_delete(monkeypatch):
    captured = {}

    def fake_json(url, *, method="GET", payload=None, timeout=10):
        captured.update({"method": method, "payload": payload})
        return {}

    monkeypatch.setattr("configuration.services._ollama_json", fake_json)

    delete_ollama_model(name="llama3:8b")

    assert captured["method"] == "DELETE"
    assert captured["payload"] == {"name": "llama3:8b"}


def test_delete_ollama_model_propagates_ollama_error(monkeypatch):
    def boom(*a, **k):
        raise OllamaError("Ollama returned HTTP 404")

    monkeypatch.setattr("configuration.services._ollama_json", boom)

    with pytest.raises(OllamaError, match="404"):
        delete_ollama_model(name="missing:1b")


def test_pull_ollama_model_creates_pending_and_enqueues(monkeypatch, user):
    queued: list[int] = []
    monkeypatch.setattr("configuration.services.pull_ollama_model_task.delay", queued.append)

    pull = pull_ollama_model(name="llama3:8b", user=user)

    assert pull.status == OllamaPullStatus.PENDING
    assert pull.created_by == user
    assert queued == [pull.pk]


def test_pull_ollama_model_marks_failed_when_broker_is_down(monkeypatch):
    def boom(pk):
        raise RuntimeError("redis is down")

    monkeypatch.setattr("configuration.services.pull_ollama_model_task.delay", boom)

    pull = pull_ollama_model(name="llama3:8b")

    assert pull.status == OllamaPullStatus.FAILED
    assert "redis is down" in pull.error
    assert pull.completed_at is not None


def test_pull_ollama_model_rejects_an_empty_name():
    with pytest.raises(ValueError, match="name"):
        pull_ollama_model(name="   ")


def test_list_pulls_is_newest_first(user):
    first = OllamaPull.objects.create(name="a:1b", created_by=user)
    second = OllamaPull.objects.create(name="b:1b", created_by=user)

    assert list(list_pulls(limit=20)) == [second, first]


def test_pull_status_shape(user):
    pull = OllamaPull.objects.create(
        name="llama3:8b",
        status=OllamaPullStatus.RUNNING,
        progress_percent=42,
        detail="downloading",
        completed_bytes=420,
        total_bytes=1000,
        created_by=user,
    )

    payload = pull_status(pull)

    assert payload["id"] == pull.pk
    assert payload["name"] == "llama3:8b"
    assert payload["status"] == "RUNNING"
    assert payload["progress_percent"] == 42
    assert payload["detail"] == "downloading"
    assert payload["completed_bytes"] == 420
    assert payload["total_bytes"] == 1000
    assert payload["error"] == ""
    assert set(payload) == {
        "id",
        "name",
        "status",
        "progress_percent",
        "detail",
        "completed_bytes",
        "total_bytes",
        "error",
        "created_at",
        "started_at",
        "completed_at",
    }
