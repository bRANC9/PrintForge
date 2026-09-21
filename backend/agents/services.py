from django.utils import timezone

from projects.models import Project

from .models import AgentRun, AgentRunStatus

__all__ = [
    "fail_run",
    "finish_run",
    "runs_accessible_to",
    "start_run",
]


def runs_accessible_to(user):
    """Agent runs in the workspaces ``user`` is a member of (Phase 7 scoping)."""
    return AgentRun.objects.filter(project__workspace__members__user=user).distinct()


def start_run(*, project: Project, user_prompt: str) -> AgentRun:
    return AgentRun.objects.create(
        project=project,
        user_prompt=user_prompt,
        status=AgentRunStatus.RUNNING,
        started_at=timezone.now(),
    )


def finish_run(*, run: AgentRun, state: dict | None = None) -> AgentRun:
    run.status = AgentRunStatus.DONE
    run.state_json = state or run.state_json
    run.completed_at = timezone.now()
    run.save(update_fields=["status", "state_json", "completed_at"])
    return run


def fail_run(*, run: AgentRun, error: str) -> AgentRun:
    run.status = AgentRunStatus.FAILED
    run.error = error
    run.completed_at = timezone.now()
    run.save(update_fields=["status", "error", "completed_at"])
    return run
