"""Server-rendered page views for the projects UI.

These are thin Django ``TemplateView`` shells: the actual data is fetched from
the JSON API by Alpine.js (``frontend/js``). ``projects:detail`` only passes the
project id through so the page knows which project to load.

Phase 8 adds the public community pages and the build-plate editor. The plate
page additionally receives the printer/filament/profile picklists, because the
JSON API has no list endpoint for them yet (see the frontend report).
"""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView

from printers.models import Printer
from slicers.models import FilamentProfile, PrinterProfile


class ProjectDetailView(TemplateView):
    template_name = "projects/detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["project_id"] = self.kwargs.get("pk")
        return context


class CommunityListView(TemplateView):
    """Public community library (``/community/``); data comes from the API."""

    template_name = "community/list.html"


class CommunityDetailView(TemplateView):
    """Public project view (``/community/<pk>/``); only the id is passed."""

    template_name = "community/detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["project_id"] = self.kwargs.get("pk")
        return context


class ProjectPlatesView(LoginRequiredMixin, TemplateView):
    """Build-plate editor for one project (``/projects/<pk>/plates/``)."""

    template_name = "projects/plates.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["project_id"] = self.kwargs.get("pk")
        # No JSON API endpoint lists these picklists yet, so they are injected
        # server-side for the print-job / build-plate forms.
        context["printers"] = list(Printer.objects.filter(is_active=True).values("id", "name"))
        context["filaments"] = list(FilamentProfile.objects.values("id", "name", "material"))
        context["printer_profiles"] = list(PrinterProfile.objects.values("id", "name"))
        return context
