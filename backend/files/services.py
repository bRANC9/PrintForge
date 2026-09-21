"""Storage abstraction.

Phase 1 stores files on the local filesystem (a Docker volume on TrueNAS).
The interface is intentionally narrow so a MinIO/S3 backend can be dropped in
later without touching callers (see terv.md 3. fejezet, Storage).

Storage contract (every backend returned by :func:`get_storage` implements it):

- ``exists(relative_path) -> bool``
- ``size(relative_path) -> int | None`` -- ``None`` when the file is missing
- ``read_bytes(relative_path) -> bytes``
- ``write_bytes(relative_path, data) -> Path``
- ``delete(relative_path) -> None``
"""

import logging
from pathlib import Path

from django.conf import settings

from configuration.services import get_setting

logger = logging.getLogger(__name__)


class LocalStorage:
    name = "local"

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or settings.MEDIA_ROOT)

    def path(self, relative_path: str) -> Path:
        return self.root / relative_path

    def exists(self, relative_path: str) -> bool:
        return self.path(relative_path).exists()

    def size(self, relative_path: str) -> int | None:
        """Return the file size in bytes, or ``None`` when it does not exist."""
        target = self.path(relative_path)
        if not target.exists():
            return None
        return target.stat().st_size

    def read_bytes(self, relative_path: str) -> bytes:
        return self.path(relative_path).read_bytes()

    def write_bytes(self, relative_path: str, data: bytes) -> Path:
        target = self.path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def delete(self, relative_path: str) -> None:
        target = self.path(relative_path)
        if target.exists():
            target.unlink()


def get_storage():
    """Return the configured storage backend.

    The backend name is resolved through ``configuration.services`` so a runtime
    override takes effect without a restart. If the settings singleton cannot be
    read (e.g. the database is briefly unavailable), fall back to the static
    ``settings.STORAGE_BACKEND`` so storage keeps working.
    """
    try:
        backend = get_setting("storage_backend")
    except Exception:  # noqa: BLE001 - never let settings lookup break storage
        logger.warning("Settings lookup failed; using settings.STORAGE_BACKEND", exc_info=True)
        backend = settings.STORAGE_BACKEND
    if backend == "local":
        return LocalStorage()
    raise NotImplementedError(f"Storage backend '{backend}' is not implemented yet")
