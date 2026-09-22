"""Tests for the streamed Ollama pull Celery task."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from configuration.models import OllamaPull, OllamaPullStatus
from configuration.tasks import pull_ollama_model_task
from notifications.models import Notification

pytestmark = pytest.mark.django_db

User = get_user_model()


class FakeResponse:
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
def user():
    return User.objects.create_user(
        username="puller",
        email="puller@example.com",
        password="pw",
    )


@pytest.fixture(autouse=True)
def _base_url(monkeypatch):
    monkeypatch.setattr("configuration.services.get_setting", lambda name: "http://ollama:11434")


def _stub_stream(monkeypatch, lines):
    monkeypatch.setattr(
        "configuration.tasks.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(lines),
    )


def test_task_streams_progress_and_finishes_done(monkeypatch, user):
    pull = OllamaPull.objects.create(name="llama3:8b", created_by=user)
    _stub_stream(
        monkeypatch,
        [
            b'{"status":"pulling manifest"}\n',
            b'{"status":"downloading","total":1000,"completed":500}\n',
            b'{"status":"downloading","total":1000,"completed":1000}\n',
            b'{"status":"success"}\n',
        ],
    )

    result = pull_ollama_model_task(pull.pk)

    pull.refresh_from_db()
    assert result == {"pull_id": pull.pk, "status": "done"}
    assert pull.status == OllamaPullStatus.DONE
    assert pull.progress_percent == 100
    assert pull.completed_bytes == 1000
    assert pull.total_bytes == 1000
    assert pull.started_at is not None
    assert pull.completed_at is not None
    assert Notification.objects.filter(user=user, kind="ollama_pull_done").exists()


def test_task_marks_failed_on_stream_error(monkeypatch, user):
    pull = OllamaPull.objects.create(name="missing:1b", created_by=user)
    _stub_stream(monkeypatch, [b'{"error":"model not found"}\n'])

    result = pull_ollama_model_task(pull.pk)

    pull.refresh_from_db()
    assert result == {"pull_id": pull.pk, "status": "failed"}
    assert pull.status == OllamaPullStatus.FAILED
    assert "model not found" in pull.error
    assert pull.completed_at is not None
    assert Notification.objects.filter(user=user, kind="ollama_pull_failed").exists()


def test_task_handles_a_missing_pull():
    assert pull_ollama_model_task(999_999) == {"pull_id": 999_999, "status": "missing"}


def test_task_without_an_owner_does_not_notify(monkeypatch):
    pull = OllamaPull.objects.create(name="llama3:8b", created_by=None)
    _stub_stream(monkeypatch, [b'{"status":"success"}\n'])

    result = pull_ollama_model_task(pull.pk)

    assert result["status"] == "done"
    assert Notification.objects.count() == 0


def test_notification_failure_does_not_change_the_outcome(monkeypatch, user):
    pull = OllamaPull.objects.create(name="llama3:8b", created_by=user)
    _stub_stream(monkeypatch, [b'{"status":"success"}\n'])

    def boom(**kwargs):
        raise RuntimeError("notifications are down")

    monkeypatch.setattr("configuration.tasks.notify", boom)

    result = pull_ollama_model_task(pull.pk)

    pull.refresh_from_db()
    assert result["status"] == "done"
    assert pull.status == OllamaPullStatus.DONE
