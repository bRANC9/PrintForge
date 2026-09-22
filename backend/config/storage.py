"""Static files storage.

WhiteNoise's compressed + manifest storage gives gzip on the wire and a content
hash in every filename (``app.<hash>.js``), which is what makes cache-busting
work: a new build emits new URLs, so browsers never serve a stale asset after a
deploy.

Django's manifest storage raises ``ValueError`` in two situations that must not
break a template render:

* the file has no manifest entry (``collectstatic`` has not run: the test
  suite, a fresh checkout, CI) -- handled by ``manifest_strict = False``;
* the file is not present in ``STATIC_ROOT`` at all, which is what happens when
  a *new* asset is added but not yet collected. ``manifest_strict`` does not
  cover this, because the fallback still hashes the file from ``STATIC_ROOT``
  and raises when it is missing -- handled by :meth:`stored_name` below.

With both, the hashed filenames are kept in production while rendering never
depends on a collectstatic run.
"""

from whitenoise.storage import CompressedManifestStaticFilesStorage


class ForgivingCompressedManifestStaticFilesStorage(CompressedManifestStaticFilesStorage):
    """Compressed + hashed in production, tolerant when an asset is uncollected."""

    manifest_strict = False

    def stored_name(self, name: str) -> str:
        """Return the hashed name, or ``name`` when the asset cannot be hashed.

        The fallback covers an asset that is not yet in ``STATIC_ROOT`` (e.g. a
        newly added file before ``collectstatic``); the original URL is still
        served by WhiteNoise in development and by the manifest in production.
        """
        try:
            return super().stored_name(name)
        except ValueError:
            return name
