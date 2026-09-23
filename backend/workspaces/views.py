"""Server-rendered workspace-first navigation pages (docs/workspace-navigation.md).

The pages are thin ``TemplateView`` shells: the actual data is fetched from the
JSON API by Alpine.js (``frontend/js``). ``WorkspaceDetailView`` only passes the
workspace id through so the page knows which workspace to load.
"""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView


class WorkspaceListView(LoginRequiredMixin, TemplateView):
    """The home page (``/``): the caller's workspaces."""

    template_name = "workspaces/list.html"


class WorkspaceDetailView(LoginRequiredMixin, TemplateView):
    """One workspace's items/projects (``/workspaces/<pk>/``)."""

    template_name = "workspaces/detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["workspace_id"] = self.kwargs.get("pk")
        return context
