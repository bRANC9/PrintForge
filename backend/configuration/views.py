"""Server-rendered page view for the runtime settings UI.

Thin ``TemplateView`` shell: the actual values are read/written through the
JSON API (``/api/v1/settings/``) by Alpine.js. Staff only.
"""

from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.views.generic import TemplateView


class SettingsView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = "configuration/settings.html"

    def test_func(self) -> bool:
        user = self.request.user
        return bool(user.is_authenticated and user.is_staff)
