"""Template context processors for shared, non-model view data.

Only the build identity lives here: the footer shows which commit the running
image was built from (``settings.APP_GIT_SHA``, baked in by the Dockerfile's
build arg), so an operator can identify a deployment from the browser alone.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

__all__ = ["build_info"]


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
