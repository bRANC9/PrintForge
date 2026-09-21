from django.urls import path

from .views import SettingsView

app_name = "configuration"

urlpatterns = [
    path("", SettingsView.as_view(), name="settings"),
]
