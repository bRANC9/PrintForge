from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import ProjectViewSet, WorkspaceViewSet, health

router = DefaultRouter()
router.register("workspaces", WorkspaceViewSet, basename="workspace")
router.register("projects", ProjectViewSet, basename="project")

urlpatterns = [
    path("health/", health, name="api-health"),
    path("", include(router.urls)),
]
