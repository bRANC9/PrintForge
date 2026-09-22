"""Static files storage.

WhiteNoise's compressed + manifest storage gives gzip on the wire and a content
hash in every filename (``app.<hash>.js``), which is what makes cache-busting
work: a new build emits new URLs, so browsers never serve a stale asset after a
deploy.

Django's manifest storage raises ``ValueError: Missing staticfiles manifest
entry`` when a file is not in the manifest -- which is the case whenever
``collectstatic`` has not run: the test suite, a fresh checkout, CI. That turns
every template render into a hard failure.

``manifest_strict = False`` keeps the hashed filenames in production while
falling back to the original name instead of raising, so rendering never depends
on a collectstatic run.
"""

from whitenoise.storage import CompressedManifestStaticFilesStorage


class ForgivingCompressedManifestStaticFilesStorage(CompressedManifestStaticFilesStorage):
    """Compressed + hashed in production, tolerant when the manifest is absent."""

    manifest_strict = False
