"""Thin JSON API views (``/api/v1``).

Pattern: validate with a serializer -> call ``services.py`` -> serialize the
result. No business logic here. The very same service functions are called by
the MCP tools (terv.md 25. fejezet).

Phase 7: every endpoint requires authentication (except ``/api/v1/health/``)
and every queryset is scoped to the workspaces the caller is a member of.
Object-level roles are enforced by :class:`api.permissions.WorkspaceScopePermission`.
"""

import hashlib
import inspect
import uuid
from http import HTTPStatus

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import mixins, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from agents.models import AgentRun
from agents.services import runs_accessible_to
from agents.tasks import run_agent_workflow
from configuration.models import OllamaPull
from configuration.services import (
    OllamaError,
    RemoteCatalogError,
    delete_ollama_model,
    effective_settings,
    get_setting,
    get_settings,
    is_secret_setting,
    list_ollama_library_models,
    list_ollama_models,
    list_pulls,
    model_recommendations,
    ollama_version,
    pull_ollama_model,
    pull_status,
    search_huggingface_models,
    test_ollama,
    update_settings,
)
from designs.models import ModelVersion
from designs.services import (
    ARTIFACT_CONTENT_TYPES,
    ClarificationError,
    RenderEnqueueError,
    answer_clarifications,
    create_annotation_edit,
    create_next_version,
    read_artifact,
    regenerate_version,
    start_render,
    version_status,
    versions_accessible_to,
    versions_for_project,
)
from files.services import get_storage
from notifications.models import Notification
from notifications.services import (
    mark_all_read,
    mark_read,
    notifications_for_user,
    unread_count,
)
from printers.base import PrinterBackendError
from printers.models import Printer
from printers.permissions import IsPrinterOperator
from printers.services import (
    QueueError,
    cancel_job,
    enqueue_job,
    jobs_for_user,
    printer_cfs_slots,
    printer_status,
    start_job,
)
from printers.services import transition as transition_print_job
from projects.models import Project, Tag
from projects.services import (
    add_plate_item,
    build_plates_for_user,
    create_build_plate,
    create_project,
    create_share,
    generate_project_description,
    mark_project_printed,
    project_download_url,
    project_rating_summary,
    publish_project,
    rate_project,
    record_download,
    remove_plate_item,
    search_public_projects,
    set_project_description,
    set_project_skills,
    set_project_tags,
    unpublish_project,
    unrate_project,
)
from projects.services import revoke_share as revoke_project_share
from skills.services import (
    create_skill,
    delete_skill,
    skills_visible_to,
    update_skill,
)
from workspaces.models import Workspace, WorkspaceRole
from workspaces.services import create_workspace, workspaces_for_user

from .permissions import BuiltinReadOnly, IsStaff, WorkspaceScopePermission
from .serializers import (
    AgentRunSerializer,
    AnnotationEditSerializer,
    BuildPlateSerializer,
    ClarificationAnswersSerializer,
    CommunityProjectSerializer,
    ModelVersionSerializer,
    NotificationSerializer,
    OllamaModelNameSerializer,
    OllamaRecommendationQuerySerializer,
    OllamaRemoteQuerySerializer,
    PlateItemSerializer,
    PrinterSerializer,
    PrintJobCreateSerializer,
    PrintJobSerializer,
    PrintJobTransitionSerializer,
    ProjectSerializer,
    ProjectShareCreateSerializer,
    ProjectShareSerializer,
    RatingWriteSerializer,
    SettingsUpdateSerializer,
    SkillSerializer,
    TagSerializer,
    VersionCreateSerializer,
    VersionRegenerateSerializer,
    WorkspaceSerializer,
)


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    """Liveness probe used by Docker healthchecks (the only public endpoint)."""
    return Response({"status": "ok"})


def _hash_client_ip(request) -> str:
    """Return a salted-less SHA-256 of the client IP (never the raw address).

    The ``ip_hash`` is used only for coarse download de-duplication, so hashing
    at the request boundary keeps the service layer free of HTTP concerns.
    """
    ip = (request.META.get("REMOTE_ADDR") or "").strip()
    if not ip:
        return ""
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()


#: Browser-generated id used to de-duplicate anonymous downloads/prints. It is
#: deliberately opaque and short-lived; the server never trusts it for anything
#: beyond counting.
_VISITOR_ID_MAX_LENGTH = 64


def _visitor_id(request) -> str:
    """Read the anonymous visitor id from the ``X-Visitor-Id`` header.

    Missing or oversized values degrade to ``""`` (which means "count every
    request" for anonymous callers).
    """
    value = (request.headers.get("X-Visitor-Id") or "").strip()
    if not value or len(value) > _VISITOR_ID_MAX_LENGTH:
        return ""
    return value


def _agent_workflow_declares(name: str) -> bool:
    """Whether ``agents.tasks.run_agent_workflow`` declares parameter ``name``.

    A Celery ``Task``'s ``__call__`` is ``(*args, **kwargs)``, so the real
    signature is read from the task's underlying ``run`` function. A parameter is
    considered supported when it is declared or the task accepts ``**kwargs``.
    Forwarding an unsupported kwarg would not fail at ``.delay()`` (it only
    serialises) but later, inside the worker, with a ``TypeError`` -- hence the
    probe.
    """
    from agents.tasks import run_agent_workflow

    target = getattr(run_agent_workflow, "run", run_agent_workflow)
    try:
        parameters = inspect.signature(target).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables only
        return False
    if name in parameters:
        return True
    return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())


def _agent_workflow_skill_kwargs(data) -> dict:
    """Skill kwargs ``agents.tasks.run_agent_workflow`` actually declares.

    ``skill_ids`` / ``auto_skill_selection`` are forwarded only when the task
    declares them (or accepts ``**kwargs``); see
    :func:`_agent_workflow_declares`.
    """
    kwargs: dict = {}
    if _agent_workflow_declares("skill_ids"):
        kwargs["skill_ids"] = list(data.get("skill_ids") or [])
    if _agent_workflow_declares("auto_skill_selection"):
        kwargs["auto_skill_selection"] = bool(data.get("auto_skill_selection", False))
    return kwargs


def _agent_workflow_clarify_kwargs(data) -> dict:
    """``clarify_policy`` kwarg, forwarded only when the task declares it.

    ``clarify`` (``"ask"`` / ``"assume"``) is the user's per-run clarification
    policy (docs/planner-clarification.md 5.). The kwarg now exists on
    ``run_agent_workflow``; the probe keeps this safe against a task that has not
    been updated yet (a legacy task simply falls back to its own default).
    """
    if not _agent_workflow_declares("clarify_policy"):
        return {}
    return {"clarify_policy": data.get("clarify", "assume")}


def _store_reference_upload(project, uploaded) -> str:
    """Persist an uploaded reference photo and return its storage path.

    The bytes are written before the agent task is enqueued, so the task can
    read them from storage without a placeholder ``ModelVersion``. The path is
    scoped per project and made unique so two uploads never collide.
    """
    name = (getattr(uploaded, "name", "") or "reference").rsplit("/", 1)[-1]
    key = f"reference_uploads/{project.pk}/{uuid.uuid4().hex}_{name}"
    uploaded.seek(0)
    get_storage().write_bytes(key, uploaded.read())
    return key


class WorkspaceViewSet(viewsets.ModelViewSet):
    serializer_class = WorkspaceSerializer
    permission_classes = [WorkspaceScopePermission]
    # Any authenticated user may create a workspace (they become its OWNER).
    permission_open_create = True
    # Managing an existing workspace / its members is ADMIN+.
    permission_write_role = WorkspaceRole.ADMIN

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return Workspace.objects.none()
        return workspaces_for_user(user).select_related("owner")

    def perform_create(self, serializer):
        # owner comes from the session, never from the payload.
        serializer.instance = create_workspace(
            name=serializer.validated_data["name"],
            owner=self.request.user,
        )


class ProjectViewSet(viewsets.ModelViewSet):
    serializer_class = ProjectSerializer
    permission_classes = [WorkspaceScopePermission]

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return Project.objects.none()
        queryset = (
            Project.objects.filter(workspace__members__user=user)
            .select_related("workspace", "created_by")
            .prefetch_related("tags", "skills")
            .distinct()
        )
        # Workspace-first navigation (docs/workspace-navigation.md 3.): the
        # membership scope is always applied first, then narrowed by workspace.
        workspace_id = self.request.query_params.get("workspace")
        if workspace_id:
            try:
                workspace_id = int(workspace_id)
            except (TypeError, ValueError):
                return Project.objects.none()
            queryset = queryset.filter(workspace_id=workspace_id)
        return queryset

    def perform_create(self, serializer):
        data = serializer.validated_data
        project = create_project(
            workspace=data["workspace"],
            name=data["name"],
            description=data.get("description", ""),
            created_by=self.request.user,
        )
        if data.get("license"):
            project.license = data["license"]
            project.save(update_fields=["license", "updated_at"])
        if "tags" in data:
            set_project_tags(project, data["tags"])
        if data.get("skills"):
            set_project_skills(project, data["skills"])
        serializer.instance = project

    def perform_update(self, serializer):
        # Tags are M2M names and the description carries provenance, so both are
        # applied through the service layer (never by DRF's default ``tags.set()``,
        # which would expect primary keys, or a bare field write, which would
        # leave ``description_source`` stale and let the AI overwrite user text).
        # Skills are persisted through the service too, keeping the M2M write in
        # one place for the API and the MCP tools.
        tags = serializer.validated_data.pop("tags", None)
        description = serializer.validated_data.pop("description", None)
        skills = serializer.validated_data.pop("skills", None)
        instance = serializer.save()
        if description is not None:
            set_project_description(instance, description)
        if tags is not None:
            set_project_tags(instance, tags)
        if skills is not None:
            set_project_skills(instance, skills)

    @action(detail=True, methods=["get", "post"], url_path="versions")
    def versions(self, request, pk=None):
        """List versions, or generate the next one.

        A request with an explicit ``specification_json`` renders that
        specification directly (advanced/manual). A prompt-only request (with an
        optional reference photo) goes through the AI workflow instead: the
        agent turns the prompt into a validated specification and creates the
        version itself, so the response is ``202`` with no version id yet.
        """
        project = self.get_object()

        if request.method == "GET":
            serializer = ModelVersionSerializer(versions_for_project(project), many=True)
            return Response(serializer.data)

        payload = VersionCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        if data.get("specification_json") is None:
            return self._generate_with_agent(request, project, data)

        version = create_next_version(
            project=project,
            prompt=data.get("prompt", ""),
            specification=data.get("specification_json"),
            created_by=request.user,
            reference_note=data.get("reference_note", ""),
            reference_image=data.get("reference_image"),
        )
        try:
            start_render(version)
        except RenderEnqueueError as exc:
            # Broker down: 503 (retryable) instead of a 500.
            return Response({"detail": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
        return Response(
            ModelVersionSerializer(version).data,
            status=HTTPStatus.CREATED,
        )

    def _generate_with_agent(self, request, project, data) -> Response:
        """Enqueue the AI workflow for a prompt-only generation request."""
        reference_image = data.get("reference_image")
        reference_image_name = ""
        if reference_image is not None:
            reference_image_name = _store_reference_upload(project, reference_image)
        kwargs = {
            "reference_image_name": reference_image_name,
            "reference_note": data.get("reference_note", ""),
        }
        # Per-run skill selection (docs/skills.md 3.), forwarded only once the
        # agent workflow declares the kwargs (see _agent_workflow_skill_kwargs).
        kwargs.update(_agent_workflow_skill_kwargs(data))
        # Per-run clarification policy (docs/planner-clarification.md 5.).
        kwargs.update(_agent_workflow_clarify_kwargs(data))
        try:
            run_agent_workflow.delay(
                project.pk,
                data.get("prompt", ""),
                request.user.pk,
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - the broker can fail in many ways
            return Response(
                {
                    "detail": (
                        "The generation queue is unavailable, the model could not be "
                        f"queued. Please try again later. ({type(exc).__name__})"
                    )
                },
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
        return Response(
            {"status": "queued", "mode": "agent"},
            status=HTTPStatus.ACCEPTED,
        )

    @action(detail=True, methods=["post"], url_path="publish")
    def publish(self, request, pk=None):
        """Make the project public in the community library (MEMBER+)."""
        project = self.get_object()
        return Response(ProjectSerializer(publish_project(project)).data)

    @action(detail=True, methods=["post"], url_path="unpublish")
    def unpublish(self, request, pk=None):
        """Hide the project from the community library (MEMBER+)."""
        project = self.get_object()
        return Response(ProjectSerializer(unpublish_project(project)).data)

    @action(detail=True, methods=["get", "post"], url_path="download")
    def download(self, request, pk=None):
        """Return the STL URL and record the download (VIEWER+ for GET)."""
        project = self.get_object()
        version_id = None
        if request.method == "POST":
            version_id = request.data.get("model_version")
        version_id = version_id or request.query_params.get("model_version")
        model_version = None
        if version_id:
            model_version = get_object_or_404(versions_for_project(project), pk=version_id)
        url = project_download_url(project, model_version)
        if url is None:
            return Response(
                {"detail": "No STL artifact is available for this project."},
                status=HTTPStatus.NOT_FOUND,
            )
        download, counted = record_download(
            project=project,
            model_version=model_version,
            user=request.user if request.user.is_authenticated else None,
            ip_hash=_hash_client_ip(request),
            visitor_id=_visitor_id(request),
        )
        return Response(
            {
                "url": url,
                "download_id": download.pk,
                "counted": counted,
                "download_count": project.download_count,
            }
        )

    @action(detail=True, methods=["post"], url_path="print")
    def mark_printed(self, request, pk=None):
        """Register „én is nyomtattam” (MEMBER+), deduped per user/visitor."""
        project = self.get_object()
        _print_row, counted = mark_project_printed(
            project,
            user=request.user if request.user.is_authenticated else None,
            visitor_id=_visitor_id(request),
        )
        return Response({"counted": counted, "print_count": project.print_count})

    @action(detail=True, methods=["get", "post", "delete"], url_path="rate")
    def rate(self, request, pk=None):
        """Read (GET), upsert (POST) or delete (DELETE) the caller's rating (MEMBER+)."""
        project = self.get_object()
        if request.method == "DELETE":
            unrate_project(project, request.user)
        elif request.method == "POST":
            payload = RatingWriteSerializer(data=request.data)
            payload.is_valid(raise_exception=True)
            try:
                rate_project(project, request.user, payload.validated_data["score"])
            except ValueError as exc:
                return Response({"detail": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        mine = project.ratings.filter(user=request.user).values_list("score", flat=True).first()
        return Response({"summary": project_rating_summary(project), "mine": mine})

    @action(detail=True, methods=["get", "post"], url_path="share")
    def share(self, request, pk=None):
        """List shares or create a user/token share (VIEWER+ read, MEMBER+ write)."""
        project = self.get_object()
        if request.method == "GET":
            shares = project.shares.select_related("shared_with", "created_by")
            return Response(ProjectShareSerializer(shares, many=True).data)
        payload = ProjectShareCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            share = create_share(
                project,
                user=payload.validated_data.get("user"),
                create_link=payload.validated_data.get("create_link", False),
                created_by=request.user,
                expires_at=payload.validated_data.get("expires_at"),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return Response(ProjectShareSerializer(share).data, status=HTTPStatus.CREATED)

    @action(detail=True, methods=["delete"], url_path=r"shares/(?P<share_id>\d+)")
    def revoke_share(self, request, pk=None, share_id=None):
        """Delete one share of the project (MEMBER+)."""
        project = self.get_object()
        share = get_object_or_404(project.shares, pk=share_id)
        revoke_project_share(share)
        return Response(status=HTTPStatus.NO_CONTENT)

    @action(detail=True, methods=["post"], url_path="description-ai")
    def description_ai(self, request, pk=None):
        """Ask the LLM to fill the empty description/tags (terv.md 29.)."""
        project = self.get_object()
        force = bool(request.data.get("force", False))
        result = generate_project_description(project, force=force)
        return Response({**result, "project": ProjectSerializer(project).data})


class TagViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only tag catalogue for the community search UI (public)."""

    serializer_class = TagSerializer
    permission_classes = [AllowAny]
    queryset = Tag.objects.all()


class SkillViewSet(viewsets.ModelViewSet):
    """Skill CRUD (docs/skills.md 6.).

    Visible skills are the caller's workspace skills plus the global built-ins
    and public skills. Built-ins are read-only (:class:`api.permissions.BuiltinReadOnly`);
    creating a skill inside a workspace requires MEMBER there, enforced from the
    payload by :class:`api.permissions.WorkspaceScopePermission`.
    """

    serializer_class = SkillSerializer
    permission_classes = [WorkspaceScopePermission, BuiltinReadOnly]

    def get_queryset(self):
        params = self.request.query_params
        return skills_visible_to(
            self.request.user,
            kind=params.get("kind", ""),
            q=params.get("q", ""),
        )

    def perform_create(self, serializer):
        data = dict(serializer.validated_data)
        tags = data.pop("tags", None)
        serializer.instance = create_skill(
            **data,
            created_by=self.request.user,
            tags=tags,
        )

    def perform_update(self, serializer):
        data = dict(serializer.validated_data)
        tags = data.pop("tags", None)
        serializer.instance = update_skill(serializer.instance, tags=tags, **data)

    def perform_destroy(self, instance):
        delete_skill(instance)


class CommunityProjectViewSet(viewsets.ReadOnlyModelViewSet):
    """Public community library: list/retrieve only, readable anonymously.

    The queryset is hard-filtered to ``is_public=True``; there is deliberately
    no workspace scoping here (that is what makes it the community view).
    """

    serializer_class = CommunityProjectSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        params = self.request.query_params
        return search_public_projects(
            q=params.get("q", ""),
            tag_slug=params.get("tag", ""),
            license=params.get("license", ""),
            ordering=params.get("ordering", "newest"),
        )


class BuildPlateViewSet(viewsets.ModelViewSet):
    """Build-plate layouts, scoped to the caller's workspaces via ``project``."""

    serializer_class = BuildPlateSerializer
    permission_classes = [WorkspaceScopePermission]

    def get_queryset(self):
        return build_plates_for_user(self.request.user)

    def perform_create(self, serializer):
        data = serializer.validated_data
        serializer.instance = create_build_plate(
            project=data["project"],
            name=data["name"],
            created_by=self.request.user,
            printer_profile=data.get("printer_profile"),
            settings_json=data.get("settings_json"),
        )

    @action(detail=True, methods=["get", "post", "delete"], url_path="items")
    def items(self, request, pk=None):
        """List (GET), add (POST) or remove (DELETE) items on the plate."""
        plate = self.get_object()

        if request.method == "GET":
            items = plate.items.select_related("model_version")
            return Response(PlateItemSerializer(items, many=True).data)

        if request.method == "DELETE":
            item_id = request.data.get("item_id") or request.query_params.get("item_id")
            if not item_id:
                return Response(
                    {"detail": "item_id is required."},
                    status=HTTPStatus.BAD_REQUEST,
                )
            if not remove_plate_item(plate, item_id):
                return Response({"detail": "Not found."}, status=HTTPStatus.NOT_FOUND)
            return Response(status=HTTPStatus.NO_CONTENT)

        payload = PlateItemSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            item = add_plate_item(
                plate,
                model_version=payload.validated_data["model_version"],
                position_x=payload.validated_data.get("position_x", 0.0),
                position_y=payload.validated_data.get("position_y", 0.0),
                position_z=payload.validated_data.get("position_z", 0.0),
                rotation_z=payload.validated_data.get("rotation_z", 0.0),
                scale=payload.validated_data.get("scale", 1.0),
                settings_json=payload.validated_data.get("settings_json"),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return Response(PlateItemSerializer(item).data, status=HTTPStatus.CREATED)


class ModelVersionViewSet(mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Version detail, job status and artifact download.

    Only the detail route is registered (no top-level list; versions are listed
    per project via ``/projects/{id}/versions/``).
    """

    serializer_class = ModelVersionSerializer
    permission_classes = [WorkspaceScopePermission]

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return ModelVersion.objects.none()
        return versions_accessible_to(user).select_related("project", "created_by")

    @action(detail=True, methods=["get"], url_path="status")
    def status(self, request, pk=None):
        version = self.get_object()
        return Response(version_status(version))

    @action(detail=True, methods=["post"], url_path="annotations")
    def annotations(self, request, pk=None):
        """Enqueue a visual-prompt edit of this version (MEMBER+).

        The version is resolved through the workspace-scoped queryset (so a
        foreign/nonexistent id 404s); the agent task creates the derived version,
        hence the ``202`` with no new version id.
        """
        version = self.get_object()
        payload = AnnotationEditSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            create_annotation_edit(
                base_version=version,
                prompt=payload.validated_data["prompt"],
                annotations=payload.validated_data["annotations"],
                created_by=request.user,
            )
        except RenderEnqueueError as exc:
            # Broker down: 503 (retryable) instead of a 500.
            return Response({"detail": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
        return Response(
            {"queued": True, "parent_version": version.pk},
            status=HTTPStatus.ACCEPTED,
        )

    @action(detail=True, methods=["post"], url_path="regenerate")
    def regenerate(self, request, pk=None):
        """Regenerate (or manually edit) this version into a derived one (MEMBER+).

        ``specification_json`` renders directly; otherwise the agent workflow is
        enqueued with ``base_version_id`` and the UI polls the project's version
        list for the version whose ``parent_version`` is this one
        (docs/version-history-controls.md 3.).
        """
        version = self.get_object()
        payload = VersionRegenerateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        try:
            regenerate_version(
                base_version=version,
                prompt=data.get("prompt") or None,
                specification=data.get("specification_json"),
                created_by=request.user,
            )
        except RenderEnqueueError as exc:
            # Broker down: 503 (retryable) instead of a 500.
            return Response({"detail": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
        return Response(
            {"queued": True, "parent_version": version.pk},
            status=HTTPStatus.ACCEPTED,
        )

    @action(detail=True, methods=["get"], url_path=r"artifact/(?P<kind>[^/.]+)")
    def artifact(self, request, pk=None, kind=None):
        version = self.get_object()
        result = read_artifact(version, kind)
        if result is None:
            return Response({"detail": "Artifact not found."}, status=HTTPStatus.NOT_FOUND)
        relative_path, data = result
        response = HttpResponse(
            data,
            content_type=ARTIFACT_CONTENT_TYPES.get(kind, "application/octet-stream"),
        )
        filename = relative_path.rsplit("/", 1)[-1]
        # The preview PNG is rendered to be displayed in the browser (inline);
        # scad/stl stay attachments so clicking them downloads the artifact.
        disposition = "inline" if kind == "preview" else "attachment"
        response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
        return response


class AgentRunViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = AgentRunSerializer
    permission_classes = [WorkspaceScopePermission]

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return AgentRun.objects.none()
        return runs_accessible_to(user).select_related("project")

    @action(detail=True, methods=["post"], url_path="clarifications")
    def clarifications(self, request, pk=None):
        """Answer a Planner run that stopped for clarification (MEMBER+).

        The stopped run is immutable: the answers are folded into a deterministic
        prompt block and a **new** run is enqueued (docs/planner-clarification.md
        3-5.). ``409`` when the run is not waiting for answers, ``503`` when the
        Celery broker is unavailable.
        """
        run = self.get_object()
        payload = ClarificationAnswersSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            result = answer_clarifications(
                run=run,
                answers=payload.validated_data["answers"],
                created_by=request.user,
            )
        except ClarificationError as exc:
            return Response({"detail": str(exc)}, status=HTTPStatus.CONFLICT)
        except RenderEnqueueError as exc:
            # Broker down: 503 (retryable) instead of a 500.
            return Response({"detail": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
        return Response(result, status=HTTPStatus.ACCEPTED)


class PrintJobViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Print queue: list / enqueue / transition / start / cancel.

    Reads use the workspace-scoped ``printers.services.jobs_for_user``; every
    control action passes ``user=request.user`` so the service layer enforces
    ``PRINTER_OPERATOR`` as well (defence in depth on top of the DRF class).
    """

    serializer_class = PrintJobSerializer
    permission_classes = [WorkspaceScopePermission]

    def get_serializer_class(self):
        # ``create`` validates with the write serializer, whose declared
        # ``project`` FK is what the permission must authorize against.
        if self.action == "create":
            return PrintJobCreateSerializer
        return PrintJobSerializer

    def get_queryset(self):
        return jobs_for_user(self.request.user)

    def create(self, request, *args, **kwargs):
        payload = PrintJobCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        try:
            job = enqueue_job(
                project=data["project"],
                model_version=data.get("model_version"),
                build_plate=data.get("build_plate"),
                printer=data["printer"],
                created_by=request.user,
                priority=data.get("priority", 0),
                filament=data.get("filament"),
                slicer_profile=data.get("slicer_profile"),
                printer_profile=data.get("printer_profile"),
            )
        except QueueError as exc:
            return Response({"detail": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return Response(PrintJobSerializer(job).data, status=HTTPStatus.CREATED)

    @action(
        detail=True,
        methods=["post"],
        url_path="transition",
        permission_classes=[WorkspaceScopePermission, IsPrinterOperator],
    )
    def transition(self, request, pk=None):
        job = self.get_object()
        payload = PrintJobTransitionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            job = transition_print_job(job, payload.validated_data["status"], user=request.user)
        except QueueError as exc:
            return Response({"detail": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return Response(PrintJobSerializer(job).data)

    @action(
        detail=True,
        methods=["post"],
        url_path="start",
        permission_classes=[WorkspaceScopePermission, IsPrinterOperator],
    )
    def start(self, request, pk=None):
        job = self.get_object()
        try:
            job = start_job(job, user=request.user)
        except QueueError as exc:
            return Response({"detail": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return Response(PrintJobSerializer(job).data)

    @action(
        detail=True,
        methods=["post"],
        url_path="cancel",
        permission_classes=[WorkspaceScopePermission, IsPrinterOperator],
    )
    def cancel(self, request, pk=None):
        job = self.get_object()
        try:
            job = cancel_job(job, user=request.user)
        except QueueError as exc:
            return Response({"detail": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return Response(PrintJobSerializer(job).data)


def _cfs_slot_payload(slot) -> dict:
    """Serialise one :class:`~printers.base.CfsSlot` for the status endpoint."""
    return {
        "index": slot.index,
        "material": slot.material,
        "brand": slot.brand,
        "name": slot.name,
        "color": slot.color,
        "state": slot.state,
        "empty": slot.empty,
    }


class PrinterViewSet(viewsets.ModelViewSet):
    """Printer registry, live status and staff-only management (terv.md 13.).

    Reads are open to any authenticated user (printers are a global resource,
    not workspace-scoped); create/update/deactivate require staff, matching the
    settings/Ollama endpoints. ``host`` is visible to staff and ``api_key`` is
    write-only. A backend failure is reported as ``online: false`` with an
    ``error`` string instead of a 5xx, so the UI can poll safely.
    """

    serializer_class = PrinterSerializer
    # Global resource: authenticated is enough, no workspace role to check.
    permission_scope_exempt = True

    def get_permissions(self):
        if self.action in {"create", "update", "partial_update", "destroy"}:
            return [IsStaff()]
        return [WorkspaceScopePermission()]

    def get_queryset(self):
        return Printer.objects.all()

    def destroy(self, request, *args, **kwargs):
        """Deactivate instead of deleting: ``PrintJob`` protects the row."""
        printer = self.get_object()
        if printer.is_active:
            printer.is_active = False
            printer.save(update_fields=["is_active"])
        return Response(status=HTTPStatus.NO_CONTENT)

    @action(detail=True, methods=["get"], url_path="status")
    def status(self, request, pk=None):
        printer = self.get_object()
        payload = {"id": printer.pk, "name": printer.name, "backend": printer.backend}
        try:
            snapshot = printer_status(printer)
        except PrinterBackendError as exc:
            payload.update(
                online=False,
                state="offline",
                current_filename=None,
                progress=None,
                message="",
                error=str(exc),
                cfs_slots=[],
            )
            return Response(payload)

        payload.update(
            online=snapshot.online,
            state=snapshot.state,
            current_filename=snapshot.current_filename,
            progress=snapshot.progress,
            message=snapshot.message,
        )
        try:
            payload["cfs_slots"] = [_cfs_slot_payload(slot) for slot in printer_cfs_slots(printer)]
        except PrinterBackendError as exc:
            payload["cfs_slots"] = []
            payload["cfs_error"] = str(exc)
        return Response(payload)


class NotificationViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """The caller's own notifications only (Phase 7)."""

    serializer_class = NotificationSerializer
    permission_classes = [WorkspaceScopePermission]
    # Own-only: scoping + the read action enforce ownership, not a workspace role.
    permission_scope_exempt = True

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return Notification.objects.none()
        return notifications_for_user(user)

    @action(detail=True, methods=["post"], url_path="read")
    def read(self, request, pk=None):
        notification = mark_read(request.user, pk)
        if notification is None:
            return Response({"detail": "Not found."}, status=HTTPStatus.NOT_FOUND)
        return Response(NotificationSerializer(notification).data)

    @action(detail=False, methods=["post"], url_path="read-all")
    def read_all(self, request):
        updated = mark_all_read(request.user)
        return Response({"updated": updated, "unread": unread_count(request.user)})


#: Sentinel returned instead of a secret setting's real value.
MASKED_SETTING_VALUE = "***"
#: Fallback name-based classification for settings the configuration registry
#: does not (yet) mark with ``secret=True``. The registry is authoritative; this
#: heuristic only keeps unknown credential-like names protected.
_SECRET_SETTING_NAMES = frozenset({"openai_api_key"})
_SECRET_SETTING_SUFFIXES = ("_api_key", "_secret", "_token", "_password")


def _is_secret_setting(name: str) -> bool:
    """Whether the runtime setting ``name`` holds a credential.

    The configuration registry's first-class ``secret`` marker is consulted
    first; the name-based heuristic remains as a fallback so nothing regresses
    for settings that are not registered.
    """
    return (
        is_secret_setting(name)
        or name in _SECRET_SETTING_NAMES
        or name.endswith(_SECRET_SETTING_SUFFIXES)
    )


def _mask_secret_settings(payload: dict) -> dict:
    """Replace secret values in an ``effective_settings`` payload with ``***``.

    Only ``effective`` and ``overrides`` carry values. ``effective`` keeps the
    empty string for an unset secret so the UI can still show "not configured";
    ``overrides`` is masked whenever a DB override exists (never revealing
    whether it differs from the env/default value). ``sources`` is left intact.
    """
    for name in list(payload.get("effective", {})):
        if not _is_secret_setting(name):
            continue
        if payload["effective"].get(name):
            payload["effective"][name] = MASKED_SETTING_VALUE
        else:
            payload["effective"][name] = ""
        if payload.get("overrides", {}).get(name) is not None:
            payload["overrides"][name] = MASKED_SETTING_VALUE
    return payload


def _settings_payload() -> dict:
    """Shared response shape for ``GET``/``PATCH /api/v1/settings/``.

    ``effective_settings()`` plus the read-only ``embedding_dim`` (baked into
    the pgvector column, so never editable at runtime) and ``updated_at``.
    Secret values are masked (:func:`_mask_secret_settings`) before the payload
    leaves the API.
    """
    payload = effective_settings()
    payload["effective"]["embedding_dim"] = settings.EMBEDDING_DIM
    payload["overrides"]["embedding_dim"] = None
    payload["sources"]["embedding_dim"] = "env"
    payload["read_only"] = ["embedding_dim"]
    obj = get_settings()
    payload["updated_at"] = obj.updated_at.isoformat() if obj.updated_at else None
    return _mask_secret_settings(payload)


class SettingsAPIView(APIView):
    """Global runtime settings (staff only, not workspace-scoped)."""

    permission_classes = [IsStaff]

    def get(self, request):
        return Response(_settings_payload())

    def patch(self, request):
        serializer = SettingsUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        # A masked secret echoed back by the UI ("***") must not overwrite the
        # stored credential; a real value (or "" to clear it) still goes through.
        for name in list(data):
            if _is_secret_setting(name) and data[name] == MASKED_SETTING_VALUE:
                data.pop(name)
        try:
            update_settings(user=request.user, **data)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return Response(_settings_payload())


class TestOllamaView(APIView):
    """Probe the configured Ollama instance (staff only); never raises."""

    permission_classes = [IsStaff]

    def post(self, request):
        return Response(test_ollama())


class OllamaModelsView(APIView):
    """List / delete locally available Ollama models (staff only)."""

    permission_classes = [IsStaff]

    def get(self, request):
        # Always 200: a connection error is reported in ``error`` so the UI can
        # render it gracefully (never 500).
        try:
            base_url = str(get_setting("ollama_base_url") or "")
        except Exception:  # noqa: BLE001 - settings lookup must not 500 the UI
            base_url = ""
        version_info = ollama_version()
        version = version_info.get("version")
        error = version_info.get("error")
        models: list[dict] = []
        if error is None:
            try:
                models = list_ollama_models()
            except Exception as exc:  # noqa: BLE001 - degrade gracefully
                error = f"{type(exc).__name__}: {exc}"
        return Response(
            {"base_url": base_url, "version": version, "models": models, "error": error}
        )

    def delete(self, request):
        # Model tags contain ':' and '/', so the name travels as a query param.
        serializer = OllamaModelNameSerializer(data={"name": request.query_params.get("name", "")})
        serializer.is_valid(raise_exception=True)
        try:
            delete_ollama_model(name=serializer.validated_data["name"])
        except OllamaError as exc:
            return Response({"detail": str(exc)}, status=HTTPStatus.BAD_GATEWAY)
        return Response(status=HTTPStatus.NO_CONTENT)


class OllamaModelPullView(APIView):
    """Enqueue an Ollama model pull (staff only)."""

    permission_classes = [IsStaff]

    def post(self, request):
        serializer = OllamaModelNameSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        pull = pull_ollama_model(name=serializer.validated_data["name"], user=request.user)
        return Response(pull_status(pull), status=HTTPStatus.ACCEPTED)


class OllamaModelUseView(APIView):
    """Set a pulled model as the runtime ``ollama_model`` (staff only)."""

    permission_classes = [IsStaff]

    def post(self, request):
        serializer = OllamaModelNameSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        name = serializer.validated_data["name"]
        try:
            update_settings(user=request.user, ollama_model=name)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return Response({"ok": True, "ollama_model": name})


class OllamaRecommendationsView(APIView):
    """VRAM-based model advisor (staff only).

    Combines the curated catalog, the installed models and a memory estimate;
    never 500s when Ollama is unreachable (installed list degrades to empty).
    """

    permission_classes = [IsStaff]

    def get(self, request):
        serializer = OllamaRecommendationQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        return Response(
            model_recommendations(
                vram_gb=data["vram_gb"],
                context=data.get("context", 8192),
                category=data.get("category", ""),
            )
        )


class OllamaRemoteModelsView(APIView):
    """Proxy the ollama.com / Hugging Face model catalogs (staff only)."""

    permission_classes = [IsStaff]

    def get(self, request):
        serializer = OllamaRemoteQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        source = data["source"]
        try:
            if source == "ollama":
                models = list_ollama_library_models(limit=data["limit"])
            else:
                models = search_huggingface_models(
                    data.get("q", ""),
                    limit=min(data["limit"], 10),
                )
        except RemoteCatalogError as exc:
            return Response(
                {"source": source, "models": [], "error": str(exc)},
                status=HTTPStatus.OK,
            )
        return Response({"source": source, "models": models, "error": None})


class OllamaPullsView(APIView):
    """List the 20 most recent Ollama pulls, newest first (staff only)."""

    permission_classes = [IsStaff]

    def get(self, request):
        return Response([pull_status(pull) for pull in list_pulls(limit=20)])


class OllamaPullDetailView(APIView):
    """One Ollama pull's progress (staff only)."""

    permission_classes = [IsStaff]

    def get(self, request, pk: int):
        pull = OllamaPull.objects.filter(pk=pk).first()
        if pull is None:
            return Response({"detail": "Not found."}, status=HTTPStatus.NOT_FOUND)
        return Response(pull_status(pull))
