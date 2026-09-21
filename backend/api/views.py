from rest_framework import viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from projects.models import Project
from workspaces.models import Workspace

from .serializers import ProjectSerializer, WorkspaceSerializer


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    """Liveness probe used by Docker healthchecks."""
    return Response({"status": "ok"})


class WorkspaceViewSet(viewsets.ModelViewSet):
    queryset = Workspace.objects.select_related("owner").all()
    serializer_class = WorkspaceSerializer


class ProjectViewSet(viewsets.ModelViewSet):
    queryset = Project.objects.select_related("workspace").all()
    serializer_class = ProjectSerializer
