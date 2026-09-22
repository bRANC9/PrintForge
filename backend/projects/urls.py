from django.urls import path

from .views import ProjectDetailView, ProjectListView, ProjectPlatesView

app_name = "projects"

urlpatterns = [
    path("", ProjectListView.as_view(), name="list"),
    path("<int:pk>/", ProjectDetailView.as_view(), name="detail"),
    path("<int:pk>/plates/", ProjectPlatesView.as_view(), name="plates"),
]
