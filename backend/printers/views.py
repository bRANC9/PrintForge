"""Server-rendered page views for the printer UI.

Thin ``TemplateView`` shells: the data is fetched from the JSON API by
Alpine.js. ``printers:history`` and ``printers:list`` are login-protected.
"""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView

from .factory import supported_backends


class PrinterListView(LoginRequiredMixin, TemplateView):
    """Printer registry with live status/CFS (``/printers/``).

    The backend keys are injected because the registry is the single source of
    truth and the API has no choices endpoint.
    """

    template_name = "printers/list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["backends"] = supported_backends()
        return context


class PrintHistoryView(LoginRequiredMixin, TemplateView):
    template_name = "printers/history.html"
