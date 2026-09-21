from accounts.models import User
from workspaces.models import Workspace

from .models import Project


def create_project(
    *,
    workspace: Workspace,
    name: str,
    created_by: User,
    description: str = "",
) -> Project:
    return Project.objects.create(
        workspace=workspace,
        name=name,
        description=description,
        created_by=created_by,
    )


def projects_for_workspace(workspace: Workspace):
    return Project.objects.filter(workspace=workspace)
