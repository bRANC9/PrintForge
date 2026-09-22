"""Thin JSON API views (``/api/v1``).

Pattern: validate with a serializer -> call ``services.py`` -> serialize the
result. No business logic here. The very same service functions are called by
the MCP tools (terv.md 25. fejezet).

Phase 7: every endpoint requires authentication (except ``/api/v1/health/``)
and every queryset is scoped to the workspaces the caller is a member of.
Object-level roles are enforced by :class:`api.permissions.WorkspaceScopePermission`.
"""

import hashlib
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
from configuration.models import OllamaPull
from configuration.services import (
    OllamaError,
    delete_ollama_model,
    effective_settings,
    get_setting,
    get_settings,
    list_ollama_models,
    list_pulls,
    ollama_version,
    pull_ollama_model,
    pull_status,
    test_ollama,
    update_settings,
)
from designs.models import ModelVersion
from designs.services import (
    ARTIFACT_CONTENT_TYPES,
    RenderEnqueueError,
    create_next_version,
    read_artifact,
    start_render,
    version_status,
    versions_accessible_to,
    versions_for_project,
)
from notifications.models import Notification
from notifications.services import (
    mark_all_read,
    mark_read,
    notifications_for_user,
    unread_count,
)
from printers.permissions import IsPrinterOperator
from printers.services import (
    QueueError,
    cancel_job,
    enqueue_job,
    jobs_for_user,
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
    project_download_url,
    project_rating_summary,
    publish_project,
    rate_project,
    record_download,
    remove_plate_item,
    search_public_projects,
    set_project_description,
    set_project_tags,
    unpublish_project,
    unrate_project,
)
from projects.services import revoke_share as revoke_project_share
from workspaces.models import Workspace, WorkspaceRole
from workspaces.services import create_workspace, workspaces_for_user

from .permissions import IsStaff, WorkspaceScopePermission
from .serializers import (
    AgentRunSerializer,
    BuildPlateSerializer,
    CommunityProjectSerializer,
    ModelVersionSerializer,
    NotificationSerializer,
    OllamaModelNameSerializer,
    PlateItemSerializer,
    PrintJobCreateSerializer,
    PrintJobSerializer,
    PrintJobTransitionSerializer,
    ProjectSerializer,
    ProjectShareCreateSerializer,
    ProjectShareSerializer,
    RatingWriteSerializer,
    SettingsUpdateSerializer,
    TagSerializer,
    VersionCreateSerializer,
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
        return (
            Project.objects.filter(workspace__members__user=user)
            .select_related("workspace", "created_by")
            .prefetch_related("tags")
            .distinct()
        )

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
        serializer.instance = project

    def perform_update(self, serializer):
        # Tags are M2M names and the description carries provenance, so both are
        # applied through the service layer (never by DRF's default ``tags.set()``,
        # which would expect primary keys, or a bare field write, which would
        # leave ``description_source`` stale and let the AI overwrite user text).
        tags = serializer.validated_data.pop("tags", None)
        description = serializer.validated_data.pop("description", None)
        instance = serializer.save()
        if description is not None:
            set_project_description(instance, description)
        if tags is not None:
            set_project_tags(instance, tags)

    @action(detail=True, methods=["get", "post"], url_path="versions")
    def versions(self, request, pk=None):
        """List versions (newest first) or create the next one and render it."""
        project = self.get_object()

        if request.method == "GET":
            serializer = ModelVersionSerializer(versions_for_project(project), many=True)
            return Response(serializer.data)

        payload = VersionCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        version = create_next_version(
            project=project,
            prompt=payload.validated_data.get("prompt", ""),
            specification=payload.validated_data.get("specification_json"),
            created_by=request.user,
            reference_note=payload.validated_data.get("reference_note", ""),
            reference_image=payload.validated_data.get("reference_image"),
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
        download = record_download(
            project=project,
            model_version=model_version,
            user=request.user if request.user.is_authenticated else None,
            ip_hash=_hash_client_ip(request),
        )
        return Response(
            {
                "url": url,
                "download_id": download.pk,
                "download_count": project.download_count,
            }
        )

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
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class AgentRunViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = AgentRunSerializer
    permission_classes = [WorkspaceScopePermission]

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return AgentRun.objects.none()
        return runs_accessible_to(user).select_related("project")


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


def _settings_payload() -> dict:
    """Shared response shape for ``GET``/``PATCH /api/v1/settings/``.

    ``effective_settings()`` plus the read-only ``embedding_dim`` (baked into
    the pgvector column, so never editable at runtime) and ``updated_at``.
    """
    payload = effective_settings()
    payload["effective"]["embedding_dim"] = settings.EMBEDDING_DIM
    payload["overrides"]["embedding_dim"] = None
    payload["sources"]["embedding_dim"] = "env"
    payload["read_only"] = ["embedding_dim"]
    obj = get_settings()
    payload["updated_at"] = obj.updated_at.isoformat() if obj.updated_at else None
    return payload


class SettingsAPIView(APIView):
    """Global runtime settings (staff only, not workspace-scoped)."""

    permission_classes = [IsStaff]

    def get(self, request):
        return Response(_settings_payload())

    def patch(self, request):
        serializer = SettingsUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            update_settings(user=request.user, **serializer.validated_data)
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
