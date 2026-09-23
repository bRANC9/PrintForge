"""Server-rendered page views for the projects UI.

These are thin Django ``TemplateView`` shells: the actual data is fetched from
the JSON API by Alpine.js (``frontend/js``). ``projects:detail`` only passes the
project id through so the page knows which project to load.

Phase 8 adds the public community pages and the build-plate editor. The plate
page additionally receives the printer/filament/profile picklists, because the
JSON API has no list endpoint for them yet (see the frontend report).
"""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponse
from django.utils import timezone
from django.views.generic import TemplateView, View

from designs.services import read_artifact
from printers.models import Printer
from slicers.models import FilamentProfile, PrinterProfile

from .models import ProjectShare


def active_share(token: str) -> ProjectShare:
    """Resolve a public token share, or raise ``Http404`` when unusable.

    Only anonymous token rows (``shared_with`` null, non-empty ``token``) are
    valid public links; an expired share behaves like a missing one.
    """
    share = (
        ProjectShare.objects.select_related("project")
        .filter(token=token, shared_with__isnull=True)
        .first()
    )
    if share is None:
        raise Http404("Share link not found.")
    if share.expires_at is not None and share.expires_at <= timezone.now():
        raise Http404("Share link has expired.")
    return share


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


class SharedProjectView(TemplateView):
    """Read-only project page reached through a public token share link.

    Unlike the community pages this is *not* limited to ``is_public`` projects,
    so a private project can be shown to whoever holds the token. The page is
    server-rendered because the JSON API is workspace-scoped and would 404 for
    an anonymous visitor.
    """

    template_name = "projects/shared.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        token = self.kwargs["token"]
        project = active_share(token).project
        context["token"] = token
        context["project"] = project
        context["tag_names"] = list(project.tags.values_list("name", flat=True))
        context["license_display"] = project.get_license_display() if project.license else ""
        context["has_stl"] = project.versions.exclude(stl_file="").exists()
        return context


class SharedProjectDownloadView(View):
    """Stream the latest STL for a token share (no workspace membership needed)."""

    def get(self, request, token):
        version = (
            active_share(token).project.versions.exclude(stl_file="").order_by("-version").first()
        )
        result = read_artifact(version, "stl") if version is not None else None
        if result is None:
            raise Http404("No STL artifact.")
        relative_path, data = result
        response = HttpResponse(data, content_type="model/stl")
        filename = relative_path.rsplit("/", 1)[-1]
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


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
