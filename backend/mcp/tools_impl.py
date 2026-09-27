"""Concrete MCP tools.

Each tool is a thin wrapper over a ``services.py`` function, so the API and
MCP layers share exactly the same business logic. Tools never do HTTP and never
shell out (terv.md 25. fejezet); CAD work is only *enqueued* and runs in a
worker.

Permission note: tools are dispatched through :func:`mcp.tools.call`, the single
seam where workspace-role checks are enforced. Every tool declares an
``authorize`` gate that is invoked before the handler and delegates to
:mod:`api.permissions`, so MCP callers are subject to exactly the same roles as
the web user (terv.md 20., 25.3). Only ``create_workspace`` (open creation, like
``WorkspaceViewSet.permission_open_create``) and ``search_public_projects``
(anonymous public library) are intentionally ungated. Nothing here may bypass
the gate or re-enable shell access; ``allow_shell`` stays forbidden.
"""

import base64
import binascii

from accounts.models import User
from designs.models import ModelVersion
from designs.services import (
    MESH_SOURCE_FORMATS,
    artifact_path,
    attach_source_mesh_to_version,
    create_annotation_edit,
    create_next_version,
    create_next_version_from_mesh,
    source_mesh_path,
    source_mesh_warnings,
    start_render,
    version_status,
)
from files.services import get_storage
from printers.models import Printer
from printers.services import enqueue_job, job_target_kind
from projects.models import Project
from projects.services import (
    add_plate_item,
    create_build_plate,
    create_project,
    generate_project_description,
    mark_project_printed,
    project_rating_summary,
    projects_for_workspace,
    publish_project,
    rate_project,
    record_download,
    search_public_projects,
    set_project_tags,
    unpublish_project,
)
from slicers.models import BuildPlate, PrinterProfile
from workspaces.models import Workspace, WorkspaceRole
from workspaces.services import create_workspace

from .permissions import (
    require_build_plate_role,
    require_project_role,
    require_version_role,
    require_workspace_role_by_id,
)
from .tools import register

#: Maximum number of rows a single community search may return.
MAX_SEARCH_LIMIT = 100


# ---------------------------------------------------------------------------
# Authorization gates (registered per tool; enforced by ``mcp.tools.call``)
# ---------------------------------------------------------------------------
#
# Each gate receives the tool's keyword arguments and raises ``PermissionDenied``
# before the handler runs. The roles mirror the DRF API exactly: writes are
# MEMBER+ and reads/downloads are VIEWER+ (``ProjectViewSet``/``BuildPlateViewSet``
# use the default read=VIEWER / write=MEMBER split). Only ``create_workspace``
# (open creation, like ``WorkspaceViewSet.permission_open_create``) and
# ``search_public_projects`` (anonymous public library) are intentionally ungated.


def _workspace_member(*, workspace_id: int, user_id: int, **_kwargs) -> None:
    require_workspace_role_by_id(
        workspace_id=workspace_id,
        user_id=user_id,
        minimum=WorkspaceRole.MEMBER,
    )


def _workspace_viewer(*, workspace_id: int, user_id: int, **_kwargs) -> None:
    require_workspace_role_by_id(
        workspace_id=workspace_id,
        user_id=user_id,
        minimum=WorkspaceRole.VIEWER,
    )


def _project_member(*, project_id: int, user_id: int, **_kwargs) -> None:
    require_project_role(
        project_id=project_id,
        user_id=user_id,
        minimum=WorkspaceRole.MEMBER,
    )


def _project_viewer(*, project_id: int, user_id: int, **_kwargs) -> None:
    require_project_role(
        project_id=project_id,
        user_id=user_id,
        minimum=WorkspaceRole.VIEWER,
    )


def _version_viewer(*, version_id: int, user_id: int, **_kwargs) -> None:
    require_version_role(
        version_id=version_id,
        user_id=user_id,
        minimum=WorkspaceRole.VIEWER,
    )


def _version_member(*, base_version_id: int, user_id: int, **_kwargs) -> None:
    require_version_role(
        version_id=base_version_id,
        user_id=user_id,
        minimum=WorkspaceRole.MEMBER,
    )


def _version_member_by_id(*, version_id: int, user_id: int, **_kwargs) -> None:
    """MEMBER+ gate keyed on ``version_id`` (the detail routes' parameter name).

    Same rule as :func:`_version_member`, just for the tools that name their
    version parameter ``version_id`` like ``get_model_version`` /
    ``export_model_stl`` do.
    """
    require_version_role(
        version_id=version_id,
        user_id=user_id,
        minimum=WorkspaceRole.MEMBER,
    )


def _build_plate_member(*, build_plate_id: int, user_id: int, **_kwargs) -> None:
    require_build_plate_role(
        build_plate_id=build_plate_id,
        user_id=user_id,
        minimum=WorkspaceRole.MEMBER,
    )


@register("create_workspace", "Create a workspace owned by a user")
def create_workspace_tool(*, name: str, owner_id: int) -> dict:
    owner = User.objects.get(pk=owner_id)
    workspace = create_workspace(name=name, owner=owner)
    return {"id": workspace.id, "name": workspace.name}


@register(
    "create_project",
    "Create a project inside a workspace (requires MEMBER)",
    authorize=_workspace_member,
)
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


@register(
    "list_projects",
    "List projects in a workspace (requires VIEWER)",
    authorize=_workspace_viewer,
)
def list_projects_tool(*, workspace_id: int, user_id: int) -> list[dict]:
    workspace = Workspace.objects.get(pk=workspace_id)
    projects = projects_for_workspace(workspace)
    return [{"id": p.id, "name": p.name} for p in projects]


@register(
    "generate_model_from_prompt",
    "Create the next model version for a project and enqueue STL rendering (requires MEMBER)",
    authorize=_project_member,
)
def generate_model_from_prompt_tool(*, project_id: int, prompt: str, user_id: int) -> dict:
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
    "Return a model version's metadata plus specification/validation JSON (requires VIEWER)",
    authorize=_version_viewer,
)
def get_model_version_tool(*, version_id: int, user_id: int) -> dict:
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
    "Return the stored STL artifact (path, exists, size) for a model version (requires VIEWER)",
    authorize=_version_viewer,
)
def export_model_stl_tool(*, version_id: int, user_id: int) -> dict:
    version = ModelVersion.objects.get(pk=version_id)
    relative_path = artifact_path(version, "stl")
    if not relative_path:
        return {"path": None, "exists": False, "size": 0}

    storage = get_storage()
    size = storage.size(relative_path)
    return {"path": relative_path, "exists": size is not None, "size": size or 0}


@register(
    "edit_model_from_annotations",
    (
        "Enqueue an annotation-driven (visual prompt) edit of a model version via the "
        "agent workflow (requires MEMBER)"
    ),
    authorize=_version_member,
)
def edit_model_from_annotations_tool(
    *,
    base_version_id: int,
    prompt: str,
    annotations: list[dict],
    user_id: int,
) -> dict:
    """Mirror ``POST /api/v1/versions/{id}/annotations/`` (docs/visual-editing.md 3.4).

    Thin wrapper over :func:`designs.services.create_annotation_edit`: the agent
    task creates the derived version and records ``parent_version`` /
    ``annotations_json`` on it, so this returns the base id and ``queued=True``
    rather than a new version. Only light shape validation happens here -- the
    DRF serializer owns the full payload shape; this must not import DRF.
    ``RenderEnqueueError`` propagates, mirroring the API's ``503``.
    """
    if (
        not isinstance(annotations, list)
        or not annotations
        or not all(isinstance(annotation, dict) for annotation in annotations)
    ):
        raise ValueError("annotations must be a non-empty list of objects")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must not be empty")

    version = ModelVersion.objects.get(pk=base_version_id)
    user = User.objects.get(pk=user_id)
    create_annotation_edit(
        base_version=version,
        prompt=prompt,
        annotations=annotations,
        created_by=user,
    )
    return {
        "base_version_id": version.id,
        "project_id": version.project_id,
        "queued": True,
    }


# ---------------------------------------------------------------------------
# Mesh import: adopt an externally generated .stl/.obj/.glb as a version's source
# ---------------------------------------------------------------------------
#
# MCP speaks JSON, so the upload travels base64-encoded in ``mesh_base64`` while
# the HTTP endpoints take the same bytes as a multipart file. Both hand them to
# the same ``designs.services`` functions, which own the format / size /
# printability gate and the storage layout -- a rejected mesh raises
# ``ValueError`` (``MeshNotPrintableError``) here exactly as the endpoints answer
# ``400``, and nothing is written or queued in that case. ``RenderEnqueueError``
# propagates, mirroring the API's ``503``.
#
# That includes the zero-byte mesh: ``base64.b64decode("")`` is a valid decode of
# empty bytes, so ``mesh_base64=""`` is a plausible client mistake and reaches the
# *same* service-level empty-file ``ValueError`` (and therefore the same English
# message) the HTTP endpoints now answer with ``{"detail": ...}``.

#: Accepted extensions as they read in a sentence (``.stl, .obj, .glb``), built
#: from the registry so a tool description cannot advertise a format the gate
#: rejects -- or omit one it accepts.
_MESH_FORMATS_LABEL = ", ".join(f".{fmt}" for fmt in MESH_SOURCE_FORMATS)


def _mesh_bytes(mesh_base64: str) -> bytes:
    """Decode the base64 upload into raw mesh bytes (transport concern only)."""
    try:
        return base64.b64decode(str(mesh_base64 or ""), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("mesh_base64 must be base64-encoded mesh bytes") from exc


def _mesh_version_payload(version: ModelVersion) -> dict:
    """The version fields the mesh tools return, shaped like the API payload."""
    return {
        "version_id": version.id,
        "project_id": version.project_id,
        "version": version.version,
        "status": version_status(version)["status"],
        "source_mesh": source_mesh_path(version),
        "warnings": source_mesh_warnings(version),
    }


@register(
    "create_version_from_mesh",
    (
        f"Create the next model version of a project from an uploaded {_MESH_FORMATS_LABEL} "
        "mesh (base64 in mesh_base64) and enqueue its render (requires MEMBER)"
    ),
    authorize=_project_member,
)
def create_version_from_mesh_tool(
    *,
    project_id: int,
    user_id: int,
    filename: str,
    mesh_base64: str,
    scale_mm: float | None = None,
    rotate_deg: list[float] | None = None,
    repair: bool | None = None,
    reference_note: str = "",
) -> dict:
    """Mirror ``POST /api/v1/projects/{id}/versions/from-mesh/``.

    Thin wrapper over :func:`designs.services.create_next_version_from_mesh`
    followed by the shared :func:`designs.services.start_render` enqueue, so the
    tool and the endpoint persist the same ``manual`` version with the same
    ``generator: mesh`` specification. Omitted ``scale_mm`` / ``rotate_deg`` /
    ``repair`` stay out of the specification, so the mesh backend's own defaults
    apply.
    """
    project = Project.objects.get(pk=project_id)
    user = User.objects.get(pk=user_id)
    version = create_next_version_from_mesh(
        project=project,
        mesh_bytes=_mesh_bytes(mesh_base64),
        filename=filename,
        created_by=user,
        scale_mm=scale_mm,
        rotate_deg=rotate_deg,
        repair=repair,
        reference_note=reference_note,
    )
    start_render(version)
    return _mesh_version_payload(version)


@register(
    "attach_source_mesh_to_version",
    (
        f"Replace a model version's source mesh with an uploaded {_MESH_FORMATS_LABEL} mesh "
        "(base64 in mesh_base64) and re-render it (requires MEMBER)"
    ),
    authorize=_version_member_by_id,
)
def attach_source_mesh_to_version_tool(
    *,
    version_id: int,
    user_id: int,
    filename: str,
    mesh_base64: str,
    scale_mm: float | None = None,
    rotate_deg: list[float] | None = None,
    repair: bool | None = None,
    reference_note: str = "",
) -> dict:
    """Mirror ``POST /api/v1/versions/{id}/source-mesh/``.

    Thin wrapper over :func:`designs.services.attach_source_mesh_to_version`
    plus the enqueue: the version keeps its id and history, its specification is
    rewritten for the mesh backend and the previous mesh file is removed.
    ``user_id`` only identifies the caller for the workspace-role gate (as in
    ``get_model_version`` / ``export_model_stl``); the row already records who
    created it.
    """
    version = ModelVersion.objects.get(pk=version_id)
    version = attach_source_mesh_to_version(
        version=version,
        mesh_bytes=_mesh_bytes(mesh_base64),
        filename=filename,
        scale_mm=scale_mm,
        rotate_deg=rotate_deg,
        repair=repair,
        reference_note=reference_note,
    )
    start_render(version)
    return _mesh_version_payload(version)


@register(
    "get_source_mesh",
    "Return a model version's source mesh (path, exists, size) (requires VIEWER)",
    authorize=_version_viewer,
)
def get_source_mesh_tool(*, version_id: int, user_id: int) -> dict:
    """Mirror ``GET /api/v1/versions/{id}/source-mesh/`` (path/exists/size).

    The same shape as :func:`export_model_stl_tool` for the *input* mesh: the
    source mesh is not an artifact, so it is read through
    :func:`designs.services.source_mesh_path` instead of ``artifact_path``.
    """
    version = ModelVersion.objects.get(pk=version_id)
    relative_path = source_mesh_path(version)
    if not relative_path:
        return {"path": None, "exists": False, "size": 0}

    storage = get_storage()
    size = storage.size(relative_path)
    return {"path": relative_path, "exists": size is not None, "size": size or 0}


# ---------------------------------------------------------------------------
# Community library (terv.md Phase 8) -- same services as the JSON API
# ---------------------------------------------------------------------------


@register(
    "publish_project",
    "Publish a project to the community library (requires MEMBER)",
    authorize=_project_member,
)
def publish_project_tool(*, project_id: int, user_id: int) -> dict:
    project = publish_project(Project.objects.get(pk=project_id))
    return {"id": project.id, "name": project.name, "is_public": project.is_public}


@register(
    "unpublish_project",
    "Remove a project from the community library (requires MEMBER)",
    authorize=_project_member,
)
def unpublish_project_tool(*, project_id: int, user_id: int) -> dict:
    project = unpublish_project(Project.objects.get(pk=project_id))
    return {"id": project.id, "name": project.name, "is_public": project.is_public}


@register(
    "search_public_projects",
    "Search the public community library (anonymous)",
)
def search_public_projects_tool(
    *,
    q: str = "",
    tag_slug: str = "",
    license: str = "",
    ordering: str = "newest",
    limit: int = 20,
) -> list[dict]:
    limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
    projects = search_public_projects(
        q=q,
        tag_slug=tag_slug,
        license=license,
        ordering=ordering,
    )[:limit]
    return [_public_project(project) for project in projects]


def _public_project(project: Project) -> dict:
    average = getattr(project, "average_rating", None)
    count = getattr(project, "rating_count", 0) or 0
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "license": project.license,
        "download_count": project.download_count,
        "print_count": project.print_count,
        "rating": {
            "average": round(float(average), 2) if average is not None else None,
            "count": int(count),
        },
        "tags": [tag.name for tag in project.tags.all()],
    }


@register(
    "set_project_tags",
    "Replace a project's tags (requires MEMBER)",
    authorize=_project_member,
)
def set_project_tags_tool(*, project_id: int, user_id: int, tags: list[str]) -> dict:
    project = Project.objects.get(pk=project_id)
    applied = set_project_tags(project, tags)
    return {"id": project.id, "tags": [tag.name for tag in applied]}


@register(
    "rate_project",
    "Create or update the caller's 1-5 rating of a project (requires MEMBER)",
    authorize=_project_member,
)
def rate_project_tool(*, project_id: int, user_id: int, score: int) -> dict:
    project = Project.objects.get(pk=project_id)
    user = User.objects.get(pk=user_id)
    rating = rate_project(project, user, score)
    return {
        "project_id": project.id,
        "score": rating.score,
        "summary": project_rating_summary(project),
    }


@register(
    "record_download",
    "Record an STL download and bump the project counter (requires VIEWER)",
    authorize=_project_viewer,
)
def record_download_tool(
    *,
    project_id: int,
    user_id: int,
    model_version_id: int | None = None,
    ip_hash: str = "",
) -> dict:
    project = Project.objects.get(pk=project_id)
    user = User.objects.get(pk=user_id)
    model_version = None
    if model_version_id is not None:
        model_version = ModelVersion.objects.get(pk=model_version_id)
        if model_version.project_id != project.id:
            raise ValueError("model_version does not belong to the given project")
    download, counted = record_download(
        project,
        model_version=model_version,
        user=user,
        ip_hash=ip_hash,
    )
    return {
        "download_id": download.id,
        "project_id": project.id,
        "counted": counted,
        "download_count": project.download_count,
    }


@register(
    "mark_project_printed",
    "Register 'én is nyomtattam' once per user and bump the project counter (requires MEMBER)",
    authorize=_project_member,
)
def mark_project_printed_tool(*, project_id: int, user_id: int) -> dict:
    project = Project.objects.get(pk=project_id)
    user = User.objects.get(pk=user_id)
    _print_row, counted = mark_project_printed(project, user=user)
    return {
        "project_id": project.id,
        "counted": counted,
        "print_count": project.print_count,
    }


@register(
    "generate_project_description",
    "Ask the LLM to fill the empty description/tags (requires MEMBER)",
    authorize=_project_member,
)
def generate_project_description_tool(
    *, project_id: int, user_id: int, force: bool = False
) -> dict:
    project = Project.objects.get(pk=project_id)
    result = generate_project_description(project, force=force)
    return {"project_id": project.id, **result}


# ---------------------------------------------------------------------------
# Build plates (terv.md 28.) and the print queue (terv.md 14.)
# ---------------------------------------------------------------------------


@register(
    "create_build_plate",
    "Create a build plate inside a project (requires MEMBER)",
    authorize=_project_member,
)
def create_build_plate_tool(
    *,
    project_id: int,
    user_id: int,
    name: str,
    printer_profile_id: int | None = None,
    settings_json: dict | None = None,
) -> dict:
    project = Project.objects.get(pk=project_id)
    user = User.objects.get(pk=user_id)
    printer_profile = None
    if printer_profile_id is not None:
        printer_profile = PrinterProfile.objects.get(pk=printer_profile_id)
    plate = create_build_plate(
        project=project,
        name=name,
        created_by=user,
        printer_profile=printer_profile,
        settings_json=settings_json,
    )
    return {"id": plate.id, "project_id": project.id, "name": plate.name}


@register(
    "add_plate_item",
    "Add a model version to a build plate (requires MEMBER)",
    authorize=_build_plate_member,
)
def add_plate_item_tool(
    *,
    build_plate_id: int,
    user_id: int,
    model_version_id: int,
    position_x: float = 0.0,
    position_y: float = 0.0,
    position_z: float = 0.0,
    rotation_z: float = 0.0,
    scale: float = 1.0,
    settings_json: dict | None = None,
) -> dict:
    plate = BuildPlate.objects.get(pk=build_plate_id)
    model_version = ModelVersion.objects.get(pk=model_version_id)
    item = add_plate_item(
        plate,
        model_version=model_version,
        position_x=position_x,
        position_y=position_y,
        position_z=position_z,
        rotation_z=rotation_z,
        scale=scale,
        settings_json=settings_json,
    )
    return {
        "id": item.id,
        "build_plate_id": plate.id,
        "model_version_id": model_version.id,
    }


@register(
    "enqueue_print_job",
    "Queue a model version or a build plate on a printer (requires MEMBER)",
    authorize=_project_member,
)
def enqueue_print_job_tool(
    *,
    project_id: int,
    user_id: int,
    printer_id: int,
    model_version_id: int | None = None,
    build_plate_id: int | None = None,
    priority: int = 0,
) -> dict:
    project = Project.objects.get(pk=project_id)
    user = User.objects.get(pk=user_id)
    printer = Printer.objects.get(pk=printer_id)
    model_version = (
        ModelVersion.objects.get(pk=model_version_id) if model_version_id is not None else None
    )
    build_plate = BuildPlate.objects.get(pk=build_plate_id) if build_plate_id is not None else None
    job = enqueue_job(
        project=project,
        printer=printer,
        model_version=model_version,
        build_plate=build_plate,
        created_by=user,
        priority=priority,
    )
    return {"job_id": job.id, "status": job.status, "target_kind": job_target_kind(job)}
