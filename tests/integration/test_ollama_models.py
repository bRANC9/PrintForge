"""Ollama model-management integration tests (terv.md 19. fejezet).

Verifies the staff-only model API/page, graceful degradation when Ollama is
unreachable, the streamed NDJSON pull task (progress, failure, notifications),
progress-write throttling and model-name validation. No network: Ollama HTTP is
stubbed and the Celery broker is patched.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from factories import UserFactory
from rest_framework.test import APIClient

from configuration.models import OllamaPull, OllamaPullStatus
from configuration.services import OllamaError, get_setting
from configuration.tasks import pull_ollama_model_task
from notifications.models import Notification

pytestmark = pytest.mark.django_db

MODELS_URL = "/api/v1/ollama/models/"
PULL_URL = "/api/v1/ollama/models/pull/"
USE_URL = "/api/v1/ollama/models/use/"
PULLS_URL = "/api/v1/ollama/pulls/"


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """The settings singleton is cached process-wide; keep tests isolated."""
    cache.clear()
    yield
    cache.clear()


def auth(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _staff_client() -> APIClient:
    return auth(UserFactory(is_staff=True))


def _fake_ollama_json(monkeypatch, *, version="0.3.0", models=None, error=None):
    """Patch the low-level Ollama HTTP call used by the real service layer."""

    def fake(url, *, method="GET", payload=None, timeout=10):
        if error is not None:
            raise OllamaError(error)
        if url.endswith("/api/version"):
            return {"version": version}
        if url.endswith("/api/tags"):
            return {"models": models or []}
        if url.endswith("/api/show"):
            return {"modelfile": "FROM llama3"}
        return {}

    monkeypatch.setattr("configuration.services._ollama_json", fake)


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


def test_ollama_endpoints_require_authentication():
    client = APIClient()

    assert client.get(MODELS_URL).status_code in (401, 403)
    assert client.post(PULL_URL, {"name": "llama3:8b"}, format="json").status_code in (401, 403)
    assert client.post(USE_URL, {"name": "llama3:8b"}, format="json").status_code in (401, 403)
    assert client.get(PULLS_URL).status_code in (401, 403)
    assert client.get(f"{PULLS_URL}1/").status_code in (401, 403)
    assert client.delete(f"{MODELS_URL}?name=llama3:8b").status_code in (401, 403)


def test_ollama_endpoints_require_staff():
    client = auth(UserFactory())

    assert client.get(MODELS_URL).status_code == 403
    assert client.post(PULL_URL, {"name": "llama3:8b"}, format="json").status_code == 403
    assert client.post(USE_URL, {"name": "llama3:8b"}, format="json").status_code == 403
    assert client.get(PULLS_URL).status_code == 403
    assert client.get(f"{PULLS_URL}1/").status_code == 403
    assert client.delete(f"{MODELS_URL}?name=llama3:8b").status_code == 403


def test_ollama_page_is_staff_only():
    assert reverse("configuration:ollama") == "/settings/ollama/"

    anonymous = Client().get("/settings/ollama/")
    assert anonymous.status_code == 302
    assert "/login/" in anonymous["Location"]

    member_client = Client()
    member_client.force_login(UserFactory())
    assert member_client.get("/settings/ollama/").status_code == 403

    staff_client = Client()
    staff_client.force_login(UserFactory(is_staff=True))
    assert staff_client.get("/settings/ollama/").status_code == 200


def test_staff_can_use_every_ollama_endpoint(monkeypatch):
    _fake_ollama_json(monkeypatch, models=[{"name": "llama3:8b"}])
    monkeypatch.setattr("configuration.services.pull_ollama_model_task.delay", lambda pk: None)
    client = _staff_client()

    models = client.get(MODELS_URL)
    assert models.status_code == 200
    assert models.json()["models"][0]["name"] == "llama3:8b"

    pull = client.post(PULL_URL, {"name": "llama3:8b"}, format="json")
    assert pull.status_code == 202
    assert pull.json()["status"] == OllamaPullStatus.PENDING

    assert client.post(USE_URL, {"name": "llama3:8b"}, format="json").status_code == 200
    assert client.get(PULLS_URL).status_code == 200
    assert client.get(f"{PULLS_URL}{pull.json()['id']}/").status_code == 200
    assert client.delete(f"{MODELS_URL}?name=llama3:8b").status_code == 204


# ---------------------------------------------------------------------------
# Ollama unreachable: degrade, never 500
# ---------------------------------------------------------------------------


def test_list_models_is_200_when_ollama_is_unreachable(monkeypatch):
    _fake_ollama_json(monkeypatch, error="URLError: connection refused")

    response = _staff_client().get(MODELS_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["models"] == []
    assert body["version"] is None
    assert "connection refused" in body["error"]


def test_list_models_reports_a_listing_failure(monkeypatch):
    # Version probe succeeds, but /api/tags fails.
    def fake(url, *, method="GET", payload=None, timeout=10):
        if url.endswith("/api/version"):
            return {"version": "0.3.0"}
        raise OllamaError("Ollama returned HTTP 500: boom")

    monkeypatch.setattr("configuration.services._ollama_json", fake)

    response = _staff_client().get(MODELS_URL)

    assert response.status_code == 200
    assert response.json()["models"] == []
    assert "500" in response.json()["error"]


def test_pull_marks_failed_when_the_broker_is_down(monkeypatch):
    def boom(pk):
        raise RuntimeError("redis is down")

    monkeypatch.setattr("configuration.services.pull_ollama_model_task.delay", boom)

    response = _staff_client().post(PULL_URL, {"name": "llama3:8b"}, format="json")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == OllamaPullStatus.FAILED
    assert "redis is down" in body["error"]
    assert body["completed_at"] is not None


def test_delete_reports_ollama_failure_as_502(monkeypatch):
    _fake_ollama_json(monkeypatch, error="Ollama returned HTTP 404: model not found")

    response = _staff_client().delete(f"{MODELS_URL}?name=missing:1b")

    assert response.status_code == 502
    assert "404" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Streamed pull task (HTTP stubbed)
# ---------------------------------------------------------------------------


class FakeStreamResponse:
    """Minimal stand-in for the streamed ``urllib`` response."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def ollama_base_url(monkeypatch):
    monkeypatch.setattr("configuration.services.get_setting", lambda name: "http://ollama:11434")


def _stub_stream(monkeypatch, lines: list[bytes]) -> None:
    monkeypatch.setattr(
        "configuration.tasks.urllib.request.urlopen",
        lambda request, timeout: FakeStreamResponse(lines),
    )


def _ndjson(**event) -> bytes:
    return (json.dumps(event) + "\n").encode("utf-8")


def test_task_streams_progress_and_finishes_done(monkeypatch, ollama_base_url):
    user = UserFactory()
    pull = OllamaPull.objects.create(name="llama3:8b", created_by=user)
    _stub_stream(
        monkeypatch,
        [
            _ndjson(status="pulling manifest"),
            _ndjson(status="downloading", total=1000, completed=100),
            _ndjson(status="downloading", total=1000, completed=500),
            _ndjson(status="downloading", total=1000, completed=1000),
            _ndjson(status="success"),
        ],
    )

    result = pull_ollama_model_task(pull.pk)

    pull.refresh_from_db()
    assert result == {"pull_id": pull.pk, "status": "done"}
    assert pull.status == OllamaPullStatus.DONE
    assert pull.progress_percent == 100
    assert pull.completed_bytes == 1000
    assert pull.total_bytes == 1000
    assert pull.detail == "success"
    assert pull.started_at is not None
    assert pull.completed_at is not None
    assert Notification.objects.filter(user=user, kind="ollama_pull_done").exists()


def test_task_progress_is_monotonic(monkeypatch, ollama_base_url):
    from configuration import tasks

    pull = OllamaPull.objects.create(name="llama3:8b")
    _stub_stream(
        monkeypatch,
        [
            _ndjson(status="downloading", total=100, completed=10),
            _ndjson(status="downloading", total=100, completed=40),
            _ndjson(status="downloading", total=100, completed=90),
            _ndjson(status="downloading", total=100, completed=100),
        ],
    )

    seen: list[int] = []
    original = tasks._persist_progress

    def spy(p):
        seen.append(p.progress_percent)
        return original(p)

    monkeypatch.setattr(tasks, "_persist_progress", spy)

    pull_ollama_model_task(pull.pk)

    assert seen == sorted(seen)  # never goes backwards
    assert seen[-1] == 100


def test_task_mid_stream_error_marks_failed_and_truncates(monkeypatch, ollama_base_url):
    user = UserFactory()
    pull = OllamaPull.objects.create(name="missing:1b", created_by=user)
    _stub_stream(
        monkeypatch,
        [
            _ndjson(status="downloading", total=10, completed=1),
            _ndjson(error="x" * 5000),
        ],
    )

    result = pull_ollama_model_task(pull.pk)

    pull.refresh_from_db()
    assert result == {"pull_id": pull.pk, "status": "failed"}
    assert pull.status == OllamaPullStatus.FAILED
    assert pull.error.startswith("RuntimeError:")
    assert len(pull.error) <= 1000
    assert pull.completed_at is not None
    assert Notification.objects.filter(user=user, kind="ollama_pull_failed").exists()


def test_task_missing_row_is_a_noop():
    assert pull_ollama_model_task(10_000_000) == {
        "pull_id": 10_000_000,
        "status": "missing",
    }


def test_task_without_an_owner_does_not_notify(monkeypatch, ollama_base_url):
    pull = OllamaPull.objects.create(name="llama3:8b", created_by=None)
    _stub_stream(monkeypatch, [_ndjson(status="success")])

    assert pull_ollama_model_task(pull.pk)["status"] == "done"
    assert Notification.objects.count() == 0


def test_task_notifier_failure_does_not_change_a_done_outcome(monkeypatch, ollama_base_url):
    pull = OllamaPull.objects.create(name="llama3:8b", created_by=UserFactory())
    _stub_stream(monkeypatch, [_ndjson(status="success")])

    def boom(**kwargs):
        raise RuntimeError("notifications are down")

    monkeypatch.setattr("configuration.tasks.notify", boom)

    assert pull_ollama_model_task(pull.pk)["status"] == "done"
    pull.refresh_from_db()
    assert pull.status == OllamaPullStatus.DONE


def test_task_notifier_failure_does_not_change_a_failed_outcome(monkeypatch, ollama_base_url):
    pull = OllamaPull.objects.create(name="llama3:8b", created_by=UserFactory())
    _stub_stream(monkeypatch, [_ndjson(error="model not found")])

    def boom(**kwargs):
        raise RuntimeError("notifications are down")

    monkeypatch.setattr("configuration.tasks.notify", boom)

    assert pull_ollama_model_task(pull.pk)["status"] == "failed"
    pull.refresh_from_db()
    assert pull.status == OllamaPullStatus.FAILED


# ---------------------------------------------------------------------------
# Progress-write throttling
# ---------------------------------------------------------------------------


def test_task_throttles_progress_writes(monkeypatch, ollama_base_url):
    from configuration import tasks

    pull = OllamaPull.objects.create(name="llama3:8b")
    lines = [_ndjson(status="downloading", total=1000, completed=i) for i in range(1, 50)]
    lines.append(_ndjson(status="downloading", total=1000, completed=1000))
    _stub_stream(monkeypatch, lines)
    # Freeze the clock so every line lands in the same "second".
    monkeypatch.setattr("configuration.tasks.time", SimpleNamespace(monotonic=lambda: 1000.0))

    writes: list[int] = []
    original = tasks._persist_progress

    def spy(p):
        writes.append(p.progress_percent)
        return original(p)

    monkeypatch.setattr(tasks, "_persist_progress", spy)

    pull_ollama_model_task(pull.pk)

    # One write for the first line, one after the stream ends -- nowhere near
    # the 50 streamed lines.
    assert len(writes) == 2
    assert len(writes) < len(lines)
    assert writes[-1] == 100


# ---------------------------------------------------------------------------
# use endpoint
# ---------------------------------------------------------------------------


def test_use_sets_the_runtime_model():
    response = _staff_client().post(USE_URL, {"name": "qwen3-coder:7b"}, format="json")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "ollama_model": "qwen3-coder:7b"}
    assert get_setting("ollama_model") == "qwen3-coder:7b"


@pytest.mark.parametrize("name", ["", "bad name", "a" * 201])
def test_use_rejects_an_invalid_name(name):
    response = _staff_client().post(USE_URL, {"name": name}, format="json")

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Name validation / URL safety
# ---------------------------------------------------------------------------


def test_namespaced_tags_are_accepted(monkeypatch):
    monkeypatch.setattr("configuration.services.pull_ollama_model_task.delay", lambda pk: None)
    client = _staff_client()
    name = "library/llama3:8b"

    assert client.post(PULL_URL, {"name": name}, format="json").status_code == 202
    assert client.post(USE_URL, {"name": name}, format="json").status_code == 200


def test_delete_query_param_is_url_decoded(monkeypatch):
    received: list[str] = []
    monkeypatch.setattr("api.views.delete_ollama_model", lambda *, name: received.append(name))
    name = "library/llama3:8b"

    response = _staff_client().delete(f"{MODELS_URL}?name={quote(name, safe='')}")

    assert response.status_code == 204
    assert received == [name]


@pytest.mark.parametrize("name", ["../../etc/passwd", "a/../../b", "/absolute/path"])
def test_path_traversalish_names_are_rejected(monkeypatch, name):
    """Regression: '..' segments / leading slashes are rejected by the API."""
    monkeypatch.setattr("configuration.services.pull_ollama_model_task.delay", lambda pk: None)
    client = _staff_client()

    assert client.post(PULL_URL, {"name": name}, format="json").status_code == 400
    assert client.post(USE_URL, {"name": name}, format="json").status_code == 400
    assert client.delete(f"{MODELS_URL}?name={quote(name, safe='')}").status_code == 400
