"""Tests for stuck Ollama pulls: retry, cancel and the stale flag.

A pull can get stuck for two reasons: the Celery worker dies mid-download (a
container restart, an OOM kill), or the task is never picked up from the queue.
Nothing used to move such a row again, so the UI polled a download that would
never finish. These tests pin the recovery paths.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from configuration.models import OllamaPull, OllamaPullStatus
from configuration.services import (
    PULL_STALE_SECONDS,
    cancel_pull,
    pull_status,
    retry_pull,
)

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def staff(db):
    return User.objects.create_user(
        username="admin", email="admin@example.com", password="pw", is_staff=True
    )


def _pull(**kwargs) -> OllamaPull:
    defaults = {"name": "llama3:8b", "status": OllamaPullStatus.PENDING}
    defaults.update(kwargs)
    return OllamaPull.objects.create(**defaults)


def _auth(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


# ---------------------------------------------------------------------------
# Stale detection
# ---------------------------------------------------------------------------


def test_a_fresh_pending_pull_is_not_stale() -> None:
    assert pull_status(_pull())["stale"] is False


def test_a_running_pull_without_progress_is_stale() -> None:
    pull = _pull(
        status=OllamaPullStatus.RUNNING,
        started_at=timezone.now() - timedelta(seconds=PULL_STALE_SECONDS + 60),
    )

    assert pull_status(pull)["stale"] is True


def test_a_finished_pull_is_never_stale() -> None:
    """DONE/FAILED rows are history; flagging them would be noise."""
    pull = _pull(
        status=OllamaPullStatus.DONE,
        started_at=timezone.now() - timedelta(days=3),
        completed_at=timezone.now(),
    )

    assert pull_status(pull)["stale"] is False


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def test_cancel_marks_a_running_pull_cancelled() -> None:
    pull = _pull(status=OllamaPullStatus.RUNNING, started_at=timezone.now())

    cancel_pull(pull)

    pull.refresh_from_db()
    assert pull.status == OllamaPullStatus.CANCELLED
    assert pull.completed_at is not None


def test_cancel_leaves_a_finished_pull_alone() -> None:
    """Cancelling a completed download must not rewrite its outcome."""
    pull = _pull(status=OllamaPullStatus.DONE, completed_at=timezone.now())

    cancel_pull(pull)

    pull.refresh_from_db()
    assert pull.status == OllamaPullStatus.DONE


def test_cancelling_twice_is_harmless() -> None:
    pull = _pull(status=OllamaPullStatus.RUNNING)

    cancel_pull(pull)
    cancel_pull(pull)

    pull.refresh_from_db()
    assert pull.status == OllamaPullStatus.CANCELLED


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


def test_retry_resets_the_row_and_re_enqueues(monkeypatch) -> None:
    queued: list[int] = []
    monkeypatch.setattr(
        "configuration.services.pull_ollama_model_task.delay",
        lambda pk: queued.append(pk),
    )
    pull = _pull(
        status=OllamaPullStatus.FAILED,
        error="boom",
        progress_percent=37,
        completed_bytes=10,
        total_bytes=20,
        started_at=timezone.now(),
        completed_at=timezone.now(),
    )

    retry_pull(pull)

    pull.refresh_from_db()
    assert pull.status == OllamaPullStatus.PENDING
    assert pull.error == ""
    assert pull.progress_percent == 0
    assert pull.total_bytes == 0
    assert pull.started_at is None
    assert pull.completed_at is None
    assert queued == [pull.pk]


def test_retry_stuck_pending_pull(monkeypatch) -> None:
    """The exact live case: a pull that never started and never will."""
    queued: list[int] = []
    pull = _pull(
        status=OllamaPullStatus.RUNNING,
        started_at=timezone.now() - timedelta(hours=1),
    )
    monkeypatch.setattr(
        "configuration.services.pull_ollama_model_task.delay",
        lambda pk: queued.append(pk),
    )

    retry_pull(pull)

    pull.refresh_from_db()
    assert pull.status == OllamaPullStatus.PENDING
    assert queued == [pull.pk]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_api_retry_and_cancel_endpoints(staff, monkeypatch) -> None:
    monkeypatch.setattr(
        "configuration.services.pull_ollama_model_task.delay",
        lambda pk: None,
    )
    client = _auth(staff)
    pull = _pull(status=OllamaPullStatus.FAILED, error="boom")

    # A failed pull is retried, which puts it back into the pending state ...
    retry_response = client.post(f"/api/v1/ollama/pulls/{pull.pk}/retry/")
    assert retry_response.status_code == 200
    assert retry_response.json()["status"] == OllamaPullStatus.PENDING

    # ... and a pending pull can then be cancelled.
    cancel_response = client.post(f"/api/v1/ollama/pulls/{pull.pk}/cancel/")
    assert cancel_response.status_code == 200
    assert cancel_response.json()["status"] == OllamaPullStatus.CANCELLED


def test_api_rejects_unknown_action(staff) -> None:
    client = _auth(staff)
    pull = _pull()

    response = client.post(f"/api/v1/ollama/pulls/{pull.pk}/explode/")

    assert response.status_code == 404


def test_api_pull_actions_require_staff(db) -> None:
    user = User.objects.create_user(username="plain", email="p@example.com", password="pw")
    client = _auth(user)
    pull = _pull()

    assert client.post(f"/api/v1/ollama/pulls/{pull.pk}/cancel/").status_code == 403


def test_api_missing_pull_404s(staff) -> None:
    client = _auth(staff)

    assert client.post("/api/v1/ollama/pulls/999999/cancel/").status_code == 404
