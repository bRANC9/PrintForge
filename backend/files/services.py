"""Storage abstraction.

Phase 1 stores files on the local filesystem (a Docker volume on TrueNAS).
The interface is intentionally narrow so a MinIO/S3 backend can be dropped in
later without touching callers (see terv.md 3. fejezet, Storage).

Storage contract (every backend returned by :func:`get_storage` implements it):

- ``exists(relative_path) -> bool``
- ``size(relative_path) -> int | None`` -- ``None`` when the file is missing
- ``read_bytes(relative_path) -> bytes`` -- ``FileNotFoundError`` when missing
- ``write_bytes(relative_path, data) -> Path | str`` -- local path or object key
- ``delete(relative_path) -> None``

``LocalStorage`` is the default. ``S3Storage`` implements the same contract on
top of S3/MinIO (django-storages + boto3 decision); ``boto3`` is imported lazily
so merely selecting the backend never requires the optional dependency.
"""

import logging
from pathlib import Path
from typing import Any

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


def _storage_setting(name: str, default: Any = "") -> Any:
    """Resolve a storage setting: runtime override -> Django setting -> default.

    ``configuration.services.get_setting`` only knows the runtime-editable names
    (``storage_backend``); the AWS_* keys are not among them, so an unknown-key
    ``ValueError`` (or a settings backend hiccup) falls through to
    ``django.conf.settings`` via a safe ``getattr``.
    """
    try:
        value = get_setting(name)
    except Exception:  # noqa: BLE001 - unknown key / settings backend unavailable
        value = None
    if value not in (None, ""):
        return value
    return getattr(settings, name, default)


def _is_missing(exc: Exception) -> bool:
    """Whether an S3 client error means the object does not exist."""
    if isinstance(exc, FileNotFoundError):
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = str(response.get("Error", {}).get("Code", ""))
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
            return True
    return False


class S3Storage:
    """S3/MinIO storage backend implementing the narrow storage contract.

    Config is read from ``configuration.services.get_setting`` when available and
    otherwise from ``django.conf.settings`` (safe ``getattr`` defaults):
    ``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY``, ``AWS_STORAGE_BUCKET_NAME``,
    ``AWS_S3_ENDPOINT_URL`` and ``AWS_S3_REGION_NAME``. The boto3 client is built
    lazily (and can be injected) so tests and the sqlite suite never need
    credentials.
    """

    name = "s3"

    def __init__(
        self,
        *,
        client: Any | None = None,
        bucket: str | None = None,
        prefix: str = "",
    ) -> None:
        self._client = client
        self.bucket = (
            bucket
            if bucket is not None
            else str(_storage_setting("AWS_STORAGE_BUCKET_NAME", "") or "")
        )
        self.prefix = prefix or ""

    @property
    def client(self):
        """The boto3 S3 client, built on first use."""
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def _build_client(self):
        import boto3  # local import: optional dependency, never needed at import time

        return boto3.client(
            "s3",
            endpoint_url=_storage_setting("AWS_S3_ENDPOINT_URL", "") or None,
            region_name=_storage_setting("AWS_S3_REGION_NAME", "") or None,
            aws_access_key_id=_storage_setting("AWS_ACCESS_KEY_ID", "") or None,
            aws_secret_access_key=_storage_setting("AWS_SECRET_ACCESS_KEY", "") or None,
        )

    def _key(self, relative_path: str) -> str:
        return f"{self.prefix}{relative_path}" if self.prefix else relative_path

    def exists(self, relative_path: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(relative_path))
            return True
        except Exception as exc:  # noqa: BLE001 - only "missing" is a valid False
            if _is_missing(exc):
                return False
            raise

    def size(self, relative_path: str) -> int | None:
        """Return the object size in bytes, or ``None`` when it does not exist."""
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=self._key(relative_path))
        except Exception as exc:  # noqa: BLE001 - only "missing" maps to None
            if _is_missing(exc):
                return None
            raise
        return int(head["ContentLength"])

    def read_bytes(self, relative_path: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key(relative_path))
        except Exception as exc:  # noqa: BLE001 - match the local FileNotFoundError contract
            if _is_missing(exc):
                raise FileNotFoundError(relative_path) from exc
            raise
        return response["Body"].read()

    def write_bytes(self, relative_path: str, data: bytes) -> str:
        key = self._key(relative_path)
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        return key

    def delete(self, relative_path: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._key(relative_path))
        except Exception as exc:  # noqa: BLE001 - deleting a missing object is a no-op
            if _is_missing(exc):
                return
            raise


def get_storage():
    """Return the configured storage backend.

    The backend name is resolved through ``configuration.services`` so a runtime
    override takes effect without a restart. If the settings singleton cannot be
    read (e.g. the database is briefly unavailable), fall back to the static
    ``settings.STORAGE_BACKEND`` so storage keeps working. ``local`` is the
    default; ``s3`` selects :class:`S3Storage` (S3/MinIO).
    """
    try:
        backend = get_setting("storage_backend")
    except Exception:  # noqa: BLE001 - never let settings lookup break storage
        logger.warning("Settings lookup failed; using settings.STORAGE_BACKEND", exc_info=True)
        backend = settings.STORAGE_BACKEND
    if backend == "local":
        return LocalStorage()
    if backend == "s3":
        return S3Storage()
    raise NotImplementedError(f"Storage backend '{backend}' is not implemented yet")
