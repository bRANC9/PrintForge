from django.urls import path
from django.views.generic import RedirectView

from .views import ProjectDetailView, ProjectPlatesView

app_name = "projects"

urlpatterns = [
    # The flat, all-workspaces project list is gone (docs/workspace-navigation.md
    # 5.): old bookmarks are redirected to the workspace-first home. The route
    # name is kept so existing ``{% url 'projects:list' %}`` references resolve.
    path("", RedirectView.as_view(pattern_name="home", permanent=False), name="list"),
    path("<int:pk>/", ProjectDetailView.as_view(), name="detail"),
    path("<int:pk>/plates/", ProjectPlatesView.as_view(), name="plates"),
]
