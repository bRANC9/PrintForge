from django.db import transaction

from accounts.models import User
from projects.models import Project

from .models import ModelVersion


@transaction.atomic
def create_next_version(
    *,
    project: Project,
    prompt: str = "",
    created_by: User | None = None,
    specification: dict | None = None,
) -> ModelVersion:
    """Create the next version for a project (v1, v2, ...)."""
    last = project.versions.order_by("-version").first()
    next_version = (last.version + 1) if last else 1
    return ModelVersion.objects.create(
        project=project,
        version=next_version,
        prompt=prompt,
        specification_json=specification or {},
        created_by=created_by,
    )


def latest_version(project: Project) -> ModelVersion | None:
    return project.versions.order_by("-version").first()
