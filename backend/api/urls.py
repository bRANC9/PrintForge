from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AgentRunViewSet,
    BuildPlateViewSet,
    CommunityProjectViewSet,
    ModelVersionViewSet,
    NotificationViewSet,
    OllamaModelPullView,
    OllamaModelsView,
    OllamaModelUseView,
    OllamaPullDetailView,
    OllamaPullsView,
    PrintJobViewSet,
    ProjectViewSet,
    SettingsAPIView,
    TagViewSet,
    TestOllamaView,
    WorkspaceViewSet,
    health,
)

router = DefaultRouter()
router.register("workspaces", WorkspaceViewSet, basename="workspace")
router.register("projects", ProjectViewSet, basename="project")
router.register("community/projects", CommunityProjectViewSet, basename="community-project")
router.register("tags", TagViewSet, basename="tag")
router.register("build-plates", BuildPlateViewSet, basename="build-plate")
router.register("versions", ModelVersionViewSet, basename="version")
router.register("agent-runs", AgentRunViewSet, basename="agent-run")
router.register("print-jobs", PrintJobViewSet, basename="print-job")
router.register("notifications", NotificationViewSet, basename="notification")

urlpatterns = [
    path("health/", health, name="api-health"),
    path("settings/", SettingsAPIView.as_view(), name="api-settings"),
    path("settings/test-ollama/", TestOllamaView.as_view(), name="api-settings-test-ollama"),
    path("ollama/models/", OllamaModelsView.as_view(), name="api-ollama-models"),
    path("ollama/models/pull/", OllamaModelPullView.as_view(), name="api-ollama-models-pull"),
    path("ollama/models/use/", OllamaModelUseView.as_view(), name="api-ollama-models-use"),
    path("ollama/pulls/", OllamaPullsView.as_view(), name="api-ollama-pulls"),
    path("ollama/pulls/<int:pk>/", OllamaPullDetailView.as_view(), name="api-ollama-pull-detail"),
    path("", include(router.urls)),
]
