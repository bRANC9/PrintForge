from django.db import models


class AgentRunStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    RUNNING = "RUNNING", "Running"
    DONE = "DONE", "Done"
    FAILED = "FAILED", "Failed"


class AgentRun(models.Model):
    """Persisted state of one AI workflow execution."""

    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        related_name="agent_runs",
    )
    status = models.CharField(
        max_length=20,
        choices=AgentRunStatus.choices,
        default=AgentRunStatus.PENDING,
    )
    user_prompt = models.TextField(blank=True)
    state_json = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"AgentRun #{self.pk} ({self.status})"
