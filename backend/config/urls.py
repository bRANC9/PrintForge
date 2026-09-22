from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from projects.views import CommunityDetailView, CommunityListView

from .views import HomeView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", HomeView.as_view(), name="home"),
    path("community/", CommunityListView.as_view(), name="community-list"),
    path("community/<int:pk>/", CommunityDetailView.as_view(), name="community-detail"),
    path("projects/", include("projects.urls")),
    path("printers/", include("printers.urls")),
    path("settings/", include("configuration.urls")),
    path("login/", auth_views.LoginView.as_view(template_name="accounts/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("api/v1/", include("api.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
