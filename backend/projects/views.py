"""Server-rendered page views for the projects UI.

These are thin Django ``TemplateView`` shells: the actual data is fetched from
the JSON API by Alpine.js (``frontend/js``). ``projects:detail`` only passes the
project id through so the page knows which project to load.
"""

from django.views.generic import TemplateView


class ProjectListView(TemplateView):
    template_name = "projects/list.html"


class ProjectDetailView(TemplateView):
    template_name = "projects/detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["project_id"] = self.kwargs.get("pk")
        return context
