from django.urls import path

from .views import OllamaView, SettingsView

app_name = "configuration"

urlpatterns = [
    path("", SettingsView.as_view(), name="settings"),
    path("ollama/", OllamaView.as_view(), name="ollama"),
]
