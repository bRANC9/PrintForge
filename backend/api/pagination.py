"""Pagination classes for the JSON API.

DRF's default :class:`~rest_framework.pagination.PageNumberPagination` uses one
fixed page size for every endpoint. Two callers need more control:

* the notification bell, which loads a small page (10) and follows ``next`` for
  infinite scroll, and
* the poll that refreshes an already-scrolled list, which asks for as many items
  as are currently shown (``?limit=<loaded>``) so the scroll depth survives.

Both go through ``?limit=``, which is always clamped to :attr:`max_page_size` so
a caller can never request an unbounded page.
"""

from rest_framework.pagination import PageNumberPagination

__all__ = ["BoundedPageNumberPagination", "NotificationPagination"]


class BoundedPageNumberPagination(PageNumberPagination):
    """``PageNumberPagination`` that honours a clamped ``?limit=``."""

    page_size_query_param = "limit"
    max_page_size = 100

    def get_page_size(self, request):
        """Requested page size, clamped to ``1..max_page_size``.

        A missing, non-numeric or non-positive ``limit`` falls back to the
        default page size instead of raising, so a bad query parameter can never
        turn the list into a 400.
        """
        raw = request.query_params.get(self.page_size_query_param)
        if raw is None or raw == "":
            return self.page_size
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return self.page_size
        if value <= 0:
            return self.page_size
        return min(value, self.max_page_size)


class NotificationPagination(BoundedPageNumberPagination):
    """Small pages for the notification dropdown (infinite scroll)."""

    page_size = 10
    max_page_size = 100
