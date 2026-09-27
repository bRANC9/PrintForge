"""Template context processors for shared, non-model view data.

Two things live here:

* the build identity - the footer shows which commit the running image was
  built from (``settings.APP_GIT_SHA``, baked in by the Dockerfile's build
  arg), so an operator can identify a deployment from the browser alone;
* the primary navigation model, so ``base.html`` renders the header as a plain
  loop and the active section is highlighted without per-link resolver checks
  scattered through the markup.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.urls import NoReverseMatch, reverse

__all__ = ["build_info", "navigation"]


def build_info(request) -> dict[str, Any]:
    """Expose the build identity to every template.

    ``app_git_sha`` is the full value (for ``title``/copy) and
    ``app_git_sha_short`` the first 12 characters, which is what the footer
    renders. A non-git build (default ``"dev"``) is passed through unchanged.
    """
    sha = str(getattr(settings, "APP_GIT_SHA", "") or "")
    return {
        "app_git_sha": sha,
        "app_git_sha_short": sha[:12],
    }


def _matcher(
    namespaces: tuple[str, ...] = (),
    url_names: tuple[str, ...] = (),
):
    """Build a predicate that reports whether the current route is in a group.

    A group is either whole app namespaces (every view under ``workspaces/``)
    or an explicit list of route names. Grouping by namespace is what keeps a
    deep page such as an item detail highlighting "Workspaces" instead of
    dropping the highlight entirely.
    """

    def matches(resolver_match) -> bool:
        if resolver_match is None:
            return False
        if namespaces and resolver_match.namespace in namespaces:
            return True
        return resolver_match.url_name in url_names

    return matches


#: Primary navigation, in order. ``staff_only`` entries are filtered out for
#: non-staff users; ``matches`` is a callable the processor evaluates once per
#: request, so the template only reads a plain boolean.
_NAV_ITEMS: tuple[dict[str, Any], ...] = (
    {
        "key": "workspaces",
        "label": "Workspaces",
        "url_name": "workspaces:list",
        # `projects` belongs here too: an item detail page is reached from its
        # workspace, so the highlight must follow the user down into it.
        "matches": _matcher(namespaces=("workspaces", "projects"), url_names=("home",)),
    },
    {
        "key": "skills",
        "label": "Skillek",
        "url_name": "skills:list",
        "matches": _matcher(namespaces=("skills",)),
    },
    {
        "key": "community",
        "label": "Közösség",
        "url_name": "community-list",
        "matches": _matcher(url_names=("community-list", "community-detail")),
    },
    {
        "key": "printers",
        "label": "Nyomtatók",
        "url_name": "printers:list",
        "matches": _matcher(url_names=("printers:list",)),
    },
    {
        "key": "print-history",
        "label": "Nyomtatási előzmények",
        "url_name": "printers:history",
        "matches": _matcher(url_names=("printers:history",)),
    },
    {
        "key": "jobs",
        "label": "Munkák",
        "url_name": "jobs",
        "matches": _matcher(url_names=("jobs",)),
    },
    {
        "key": "settings",
        "label": "Beállítások",
        "url_name": "configuration:settings",
        "matches": _matcher(url_names=("configuration:settings",)),
        "staff_only": True,
    },
    {
        "key": "ollama",
        "label": "Ollama modellek",
        "url_name": "configuration:ollama",
        "matches": _matcher(url_names=("configuration:ollama",)),
        "staff_only": True,
    },
)


def navigation(request) -> dict[str, Any]:
    """Expose the primary navigation, flagged with the active section.

    ``base.html`` iterates ``nav_items`` and applies ``is-active`` /
    ``aria-current`` from the flag, so adding a section is a one-entry change
    here rather than a template edit plus a matching CSS rule.
    """
    resolver_match = getattr(request, "resolver_match", None)
    is_staff = bool(getattr(request.user, "is_staff", False))
    items = []
    for item in _NAV_ITEMS:
        if item.get("staff_only") and not is_staff:
            continue
        try:
            url = reverse(item["url_name"])
        except NoReverseMatch:
            # An optional app that is not installed must not break the header.
            continue
        items.append(
            {
                "key": item["key"],
                "label": item["label"],
                "url": url,
                "is_active": bool(item["matches"](resolver_match)),
            }
        )
    return {"nav_items": items}
