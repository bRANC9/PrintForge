"""Server-rendered page views for the printer UI.

Thin ``TemplateView`` shells: the data is fetched from the JSON API by
Alpine.js. ``printers:history`` and ``printers:list`` are login-protected.
"""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView


class PrinterListView(LoginRequiredMixin, TemplateView):
    """Printer registry with live status/CFS (``/printers/``)."""

    template_name = "printers/list.html"


class PrintHistoryView(LoginRequiredMixin, TemplateView):
    template_name = "printers/history.html"
