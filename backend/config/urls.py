from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from projects.views import (
    CommunityDetailView,
    CommunityListView,
    SharedProjectDownloadView,
    SharedProjectView,
)
from workspaces.views import WorkspaceListView

urlpatterns = [
    path("admin/", admin.site.urls),
    # Workspace-first navigation (docs/workspace-navigation.md): the home page
    # is the workspace list; items live under /workspaces/<id>/.
    path("", WorkspaceListView.as_view(), name="home"),
    path("workspaces/", include("workspaces.urls")),
    path("community/", CommunityListView.as_view(), name="community-list"),
    path("community/<int:pk>/", CommunityDetailView.as_view(), name="community-detail"),
    # Public token share links (Phase 7): read-only, no login required.
    path("share/<str:token>/", SharedProjectView.as_view(), name="share-detail"),
    path("share/<str:token>/stl/", SharedProjectDownloadView.as_view(), name="share-download"),
    path("projects/", include("projects.urls")),
    # Skill authoring UI (docs/skills.md 6.); the JSON CRUD lives at
    # /api/v1/skills/.
    path("skills/", include("skills.urls")),
    path("printers/", include("printers.urls")),
    path("settings/", include("configuration.urls")),
    path("login/", auth_views.LoginView.as_view(template_name="accounts/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("api/v1/", include("api.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
