"""Server-rendered page views for the printer UI.

Thin ``TemplateView`` shells: the data is fetched from the JSON API by
Alpine.js. ``printers:history`` is login-protected.
"""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView


class PrintHistoryView(LoginRequiredMixin, TemplateView):
    template_name = "printers/history.html"
