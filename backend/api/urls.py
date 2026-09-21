from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AgentRunViewSet,
    ModelVersionViewSet,
    NotificationViewSet,
    PrintJobViewSet,
    ProjectViewSet,
    SettingsAPIView,
    TestOllamaView,
    WorkspaceViewSet,
    health,
)

router = DefaultRouter()
router.register("workspaces", WorkspaceViewSet, basename="workspace")
router.register("projects", ProjectViewSet, basename="project")
router.register("versions", ModelVersionViewSet, basename="version")
router.register("agent-runs", AgentRunViewSet, basename="agent-run")
router.register("print-jobs", PrintJobViewSet, basename="print-job")
router.register("notifications", NotificationViewSet, basename="notification")

urlpatterns = [
    path("health/", health, name="api-health"),
    path("settings/", SettingsAPIView.as_view(), name="api-settings"),
    path("settings/test-ollama/", TestOllamaView.as_view(), name="api-settings-test-ollama"),
    path("", include(router.urls)),
]
