"""Celery tasks for Ollama model management (terv.md 19. fejezet).

``pull_ollama_model_task`` streams ``POST /api/pull`` (NDJSON) and persists the
progress on the :class:`~configuration.models.OllamaPull` row so the UI can poll
``/api/v1/ollama/pulls/{id}/``. DB writes are throttled (~1/s) so a long pull
does not hammer the database. The requester is notified (best-effort) when the
pull finishes.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any

from celery import shared_task
from django.utils import timezone

from notifications.services import NotificationKind, notify

from .models import OllamaPull, OllamaPullStatus

__all__ = ["pull_ollama_model_task"]

logger = logging.getLogger(__name__)

#: Hard cap for the streamed pull connection.
_OLLAMA_PULL_TIMEOUT = 3600

#: Minimum seconds between progress writes while streaming.
_PROGRESS_WRITE_INTERVAL = 1.0

#: ``OllamaPull.error`` is a TextField; keep the stored message bounded.
_MAX_ERROR_LENGTH = 1000

#: ``OllamaPull.detail`` is a CharField(300).
_MAX_DETAIL_LENGTH = 300

#: Where the notification link points.
_NOTIFICATION_URL = "/settings/ollama/"


@shared_task(name="configuration.pull_ollama_model")
def pull_ollama_model_task(pull_id: int) -> dict[str, Any]:
    """Pull an Ollama model, persisting streamed progress on ``OllamaPull``."""
    pull = OllamaPull.objects.filter(pk=pull_id).first()
    if pull is None:
        logger.warning("pull_ollama_model_task: OllamaPull %s not found", pull_id)
        return {"pull_id": pull_id, "status": "missing"}

    # Local import avoids a service <-> task import cycle.
    from .services import get_setting

    pull.status = OllamaPullStatus.RUNNING
    pull.started_at = timezone.now()
    pull.error = ""
    pull.save(update_fields=["status", "started_at", "error"])

    try:
        base_url = str(get_setting("ollama_base_url") or "").rstrip("/")
        if not base_url:
            raise RuntimeError("No Ollama base URL configured.")
        _stream_pull(pull, f"{base_url}/api/pull")
    except Exception as exc:  # noqa: BLE001 - persist *any* failure
        logger.warning("Ollama pull failed for %s: %s", pull.name, exc)
        _fail(pull, f"{type(exc).__name__}: {exc}")
        return {"pull_id": pull.pk, "status": "failed"}

    pull.status = OllamaPullStatus.DONE
    pull.progress_percent = 100
    pull.detail = (pull.detail or "success")[:_MAX_DETAIL_LENGTH]
    pull.completed_at = timezone.now()
    pull.save(update_fields=["status", "progress_percent", "detail", "completed_at"])
    _notify(pull, failed=False)
    return {"pull_id": pull.pk, "status": "done"}


def _stream_pull(pull: OllamaPull, url: str) -> None:
    """POST ``{"name", "stream": true}`` and consume the NDJSON progress lines."""
    payload = json.dumps({"name": pull.name, "stream": True}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/x-ndjson",
        },
        method="POST",
    )

    last_write = 0.0
    with urllib.request.urlopen(request, timeout=_OLLAMA_PULL_TIMEOUT) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("error"):
                raise RuntimeError(str(event["error"]))
            _apply_event(pull, event)

            now = time.monotonic()
            if now - last_write >= _PROGRESS_WRITE_INTERVAL:
                _persist_progress(pull)
                last_write = now

    _persist_progress(pull)


def _apply_event(pull: OllamaPull, event: dict) -> None:
    """Merge one streamed NDJSON event into the in-memory pull."""
    status = event.get("status")
    if status:
        pull.detail = str(status)[:_MAX_DETAIL_LENGTH]

    total = _as_int(event.get("total"))
    completed = _as_int(event.get("completed"))
    if total is not None and total > 0:
        pull.total_bytes = total
        if completed is not None:
            pull.completed_bytes = completed
            pull.progress_percent = max(0, min(100, int(completed * 100 / total)))


def _persist_progress(pull: OllamaPull) -> None:
    pull.save(
        update_fields=[
            "detail",
            "total_bytes",
            "completed_bytes",
            "progress_percent",
        ]
    )


def _fail(pull: OllamaPull, message: str) -> None:
    pull.status = OllamaPullStatus.FAILED
    pull.error = message[:_MAX_ERROR_LENGTH]
    pull.completed_at = timezone.now()
    pull.save(update_fields=["status", "error", "completed_at"])
    _notify(pull, failed=True)


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _notify(pull: OllamaPull, *, failed: bool) -> None:
    """Best-effort notification; never changes the pull outcome."""
    user = pull.created_by
    if user is None:
        return
    if failed:
        kind = NotificationKind.OLLAMA_PULL_FAILED
        message = f"Ollama model '{pull.name}' failed to pull: {pull.error}"
    else:
        kind = NotificationKind.OLLAMA_PULL_DONE
        message = f"Ollama model '{pull.name}' is ready."
    try:
        notify(user=user, message=message[:1000], kind=kind, url=_NOTIFICATION_URL)
    except Exception:  # noqa: BLE001 - notifications must never break the pull
        logger.exception("Failed to notify about OllamaPull %s", pull.pk)
