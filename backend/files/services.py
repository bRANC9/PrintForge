"""Storage abstraction.

Phase 1 stores files on the local filesystem (a Docker volume on TrueNAS).
The interface is intentionally narrow so a MinIO/S3 backend can be dropped in
later without touching callers (see terv.md 3. fejezet, Storage).
"""

from pathlib import Path

from django.conf import settings


class LocalStorage:
    name = "local"

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or settings.MEDIA_ROOT)

    def path(self, relative_path: str) -> Path:
        return self.root / relative_path

    def exists(self, relative_path: str) -> bool:
        return self.path(relative_path).exists()

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
    """Return the configured storage backend."""
    backend = settings.STORAGE_BACKEND
    if backend == "local":
        return LocalStorage()
    raise NotImplementedError(f"Storage backend '{backend}' is not implemented yet")
