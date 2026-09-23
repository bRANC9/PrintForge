from django.urls import path

from .views import WorkspaceDetailView, WorkspaceListView

app_name = "workspaces"

urlpatterns = [
    path("", WorkspaceListView.as_view(), name="list"),
    path("<int:pk>/", WorkspaceDetailView.as_view(), name="detail"),
]
