"""Server-rendered skill authoring page (docs/skills.md 6. fejezet).

A thin ``TemplateView`` shell: the skill list, the filters and the editor are
driven by Alpine.js (``frontend/static/js/skills.js``) against the JSON API
(``/api/v1/skills/``). The view deliberately holds no business logic, matching
the workspace/project page views.
"""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView


class SkillListView(LoginRequiredMixin, TemplateView):
    """Skill catalogue + editor (``/skills/``)."""

    template_name = "skills/list.html"
