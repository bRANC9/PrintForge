"""Server-rendered account pages.

Thin ``TemplateView`` shells like every other page in the app: the data comes
from the JSON API (``/api/v1/me/``) via Alpine.js, so there is no second code
path that has to be kept in sync with the API.
"""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView


class ProfileView(LoginRequiredMixin, TemplateView):
    """The caller's own profile: account fields, password, statistics."""

    template_name = "accounts/profile.html"
