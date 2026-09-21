"""Thin JSON API views (``/api/v1``).

Pattern: validate with a serializer -> call ``services.py`` -> serialize the
result. No business logic here. The very same service functions are called by
the MCP tools (terv.md 25. fejezet).

Phase 7: every endpoint requires authentication (except ``/api/v1/health/``)
and every queryset is scoped to the workspaces the caller is a member of.
Object-level roles are enforced by :class:`api.permissions.WorkspaceScopePermission`.
"""

from http import HTTPStatus

from django.http import HttpResponse
from rest_framework import mixins, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from agents.models import AgentRun
from agents.services import runs_accessible_to
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
from projects.models import Project
from projects.services import create_project
from workspaces.models import Workspace, WorkspaceRole
from workspaces.services import create_workspace, workspaces_for_user

from .permissions import WorkspaceScopePermission
from .serializers import (
    AgentRunSerializer,
    ModelVersionSerializer,
    NotificationSerializer,
    PrintJobCreateSerializer,
    PrintJobSerializer,
    PrintJobTransitionSerializer,
    ProjectSerializer,
    VersionCreateSerializer,
    WorkspaceSerializer,
)


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    """Liveness probe used by Docker healthchecks (the only public endpoint)."""
    return Response({"status": "ok"})


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
            .distinct()
        )

    def perform_create(self, serializer):
        serializer.instance = create_project(
            workspace=serializer.validated_data["workspace"],
            name=serializer.validated_data["name"],
            description=serializer.validated_data.get("description", ""),
            created_by=self.request.user,
        )

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
                model_version=data["model_version"],
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
