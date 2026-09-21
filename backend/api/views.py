"""Thin JSON API views (``/api/v1``).

Pattern: validate with a serializer -> call ``services.py`` -> serialize the
result. No business logic here. The very same service functions are called by
the MCP tools (terv.md 25. fejezet).
"""

from http import HTTPStatus

from django.http import HttpResponse
from rest_framework import mixins, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticatedOrReadOnly
from rest_framework.response import Response

from agents.models import AgentRun
from designs.models import ModelVersion
from designs.services import (
    ARTIFACT_CONTENT_TYPES,
    RenderEnqueueError,
    create_next_version,
    read_artifact,
    start_render,
    version_status,
    versions_for_project,
)
from projects.models import Project
from projects.services import create_project
from workspaces.models import Workspace
from workspaces.services import create_workspace

from .permissions import WorkspaceScopePermission
from .serializers import (
    AgentRunSerializer,
    ModelVersionSerializer,
    ProjectSerializer,
    VersionCreateSerializer,
    WorkspaceSerializer,
)


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    """Liveness probe used by Docker healthchecks."""
    return Response({"status": "ok"})


class WorkspaceViewSet(viewsets.ModelViewSet):
    queryset = Workspace.objects.select_related("owner").all()
    serializer_class = WorkspaceSerializer
    permission_classes = [IsAuthenticatedOrReadOnly, WorkspaceScopePermission]

    def perform_create(self, serializer):
        # owner comes from the session, never from the payload.
        serializer.instance = create_workspace(
            name=serializer.validated_data["name"],
            owner=self.request.user,
        )


class ProjectViewSet(viewsets.ModelViewSet):
    queryset = Project.objects.select_related("workspace", "created_by").all()
    serializer_class = ProjectSerializer
    permission_classes = [IsAuthenticatedOrReadOnly, WorkspaceScopePermission]

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

    queryset = ModelVersion.objects.select_related("project", "created_by").all()
    serializer_class = ModelVersionSerializer
    permission_classes = [IsAuthenticatedOrReadOnly, WorkspaceScopePermission]

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
    queryset = AgentRun.objects.select_related("project").all()
    serializer_class = AgentRunSerializer
    permission_classes = [IsAuthenticatedOrReadOnly, WorkspaceScopePermission]
