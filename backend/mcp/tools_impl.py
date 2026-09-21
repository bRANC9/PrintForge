"""Concrete MCP tools.

Each tool is a thin wrapper over a ``services.py`` function, so the API and
MCP layers share exactly the same business logic. Tools never do HTTP and never
shell out (terv.md 25. fejezet); CAD work is only *enqueued* and runs in a
worker.

Permission note: tools are dispatched through :func:`mcp.tools.call`, which is
the single seam where Phase 7 workspace-role checks will be enforced. Nothing
here may bypass it or re-enable shell access.
"""

from accounts.models import User
from designs.models import ModelVersion
from designs.services import (
    artifact_path,
    create_next_version,
    start_render,
    version_status,
)
from files.services import get_storage
from projects.models import Project
from projects.services import create_project, projects_for_workspace
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
    workspace = Workspace.objects.get(pk=workspace_id)
    projects = projects_for_workspace(workspace)
    return [{"id": p.id, "name": p.name} for p in projects]


@register(
    "generate_model_from_prompt",
    "Create the next model version for a project and enqueue STL rendering",
)
def generate_model_from_prompt_tool(*, project_id: int, prompt: str) -> dict:
    project = Project.objects.get(pk=project_id)
    version = create_next_version(project=project, prompt=prompt)
    start_render(version)
    return {
        "version_id": version.id,
        "version": version.version,
        "status": version_status(version)["status"],
    }


@register(
    "get_model_version",
    "Return a model version's metadata plus specification/validation JSON",
)
def get_model_version_tool(*, version_id: int) -> dict:
    version = ModelVersion.objects.get(pk=version_id)
    return {
        "version_id": version.id,
        "project_id": version.project_id,
        "version": version.version,
        "prompt": version.prompt,
        "status": version_status(version)["status"],
        "specification_json": version.specification_json or {},
        "validation_json": version.validation_json or {},
        "scad_file": version.scad_file.name or None,
        "stl_file": version.stl_file.name or None,
        "created_at": version.created_at.isoformat(),
    }


@register(
    "export_model_stl",
    "Return the stored STL artifact (path, exists, size) for a model version",
)
def export_model_stl_tool(*, version_id: int) -> dict:
    version = ModelVersion.objects.get(pk=version_id)
    relative_path = artifact_path(version, "stl")
    if not relative_path:
        return {"path": None, "exists": False, "size": 0}

    storage = get_storage()
    size = storage.size(relative_path)
    return {"path": relative_path, "exists": size is not None, "size": size or 0}
