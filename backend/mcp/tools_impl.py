"""Concrete MCP tools.

Each tool is a thin wrapper over a ``services.py`` function, so the API and
MCP layers share exactly the same business logic.
"""

from accounts.models import User
from projects.services import create_project
from workspaces.models import Workspace
from workspaces.services import create_workspace

from .tools import register


@register("create_workspace", "Create a workspace owned by a user")
def create_workspace_tool(*, name: str, owner_id: int) -> dict:
    owner = User.objects.get(pk=owner_id)
    workspace = create_workspace(name=name, owner=owner)
    return {"id": workspace.id, "name": workspace.name}


@register("create_project", "Create a project inside a workspace")
def create_project_tool(
    *, workspace_id: int, name: str, user_id: int, description: str = ""
) -> dict:
    workspace = Workspace.objects.get(pk=workspace_id)
    user = User.objects.get(pk=user_id)
    project = create_project(
        workspace=workspace,
        name=name,
        created_by=user,
        description=description,
    )
    return {"id": project.id, "name": project.name, "workspace_id": workspace.id}


@register("list_projects", "List projects in a workspace")
def list_projects_tool(*, workspace_id: int) -> list[dict]:
    projects = Workspace.objects.get(pk=workspace_id).projects.all()
    return [{"id": p.id, "name": p.name} for p in projects]
