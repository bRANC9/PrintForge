"""Orchestration: ``ModelVersion`` -> backend output -> artifacts in storage.

The Celery task in :mod:`designs.tasks` is a thin wrapper around
:func:`render_version`. Status is persisted in ``ModelVersion.validation_json``
so the API can poll it (terv.md 9. fejezet) without any new model fields:

.. code-block:: json

    {
      "status": "queued" | "running" | "done" | "failed",
      "stage": "generate" | "export" | "done" | "failed",
      "errors": ["..."],
      "warnings": [],
      "scad_file": "projects/<id>/v<version>/model.scad",
      "stl_file": "projects/<id>/v<version>/model.stl",
      "stl_bytes": 12345,
      "started_at": "...",
      "completed_at": "..."
    }

``scad_file`` is present only when the backend produced source code
(``CADBackend.produces_source_code``), so a mesh render persists ``stl_file`` and
``stl_bytes`` and leaves the other keys untouched; no key is ever renamed or
removed.

Artifacts are written through :func:`files.services.get_storage` and the
relative paths are stored on ``scad_file`` / ``stl_file``. Which artifacts are
written comes from ``CADBackend.produced_artifacts`` and the
:data:`ARTIFACT_FILENAMES` / :data:`ARTIFACT_TARGETS` maps below, which must stay
in sync with ``designs.services.ARTIFACT_KINDS`` and its ``_ARTIFACT_FIELDS``.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.utils import timezone

from .base import CADBackend, CADError, GeneratedModel
from .mesh import MeshCADBackend
from .openscad import OpenSCADBackend

if TYPE_CHECKING:  # the models module is only importable once the app registry is up
    from ..models import ModelVersion

__all__ = [
    "ARTIFACT_FILENAMES",
    "ARTIFACT_SIZE_KEYS",
    "ARTIFACT_TARGETS",
    "CAD_BACKEND_ALIASES",
    "CADValidationFailed",
    "DEFAULT_CAD_BACKEND",
    "get_backend",
    "get_storage",
    "render_version",
    "resolve_backend_name",
]

logger = logging.getLogger(__name__)

SCAD_FILENAME = "model.scad"
STL_FILENAME = "model.stl"

#: Artifact kind -> storage filename, mirroring ``designs.services.ARTIFACT_KINDS``.
ARTIFACT_FILENAMES: dict[str, str] = {
    "scad": SCAD_FILENAME,
    "stl": STL_FILENAME,
}

#: Artifact kind -> ``(storage filename, ModelVersion field, content type)``.
#: The content types match ``designs.services.ARTIFACT_CONTENT_TYPES``; the
#: filename and the field must match ``designs.services.ARTIFACT_KINDS`` /
#: its ``_ARTIFACT_FIELDS``, so ``api``/``mcp`` can serve the same layout.
ARTIFACT_TARGETS: dict[str, tuple[str, str, str]] = {
    "scad": (ARTIFACT_FILENAMES["scad"], "scad_file", "text/plain; charset=utf-8"),
    "stl": (ARTIFACT_FILENAMES["stl"], "stl_file", "model/stl"),
}

#: Artifact kind -> the ``validation_json`` key holding its byte size. Only the
#: STL reports a size; the key set of ``validation_json`` is a documented
#: contract, so no ``scad_bytes`` key is invented here.
ARTIFACT_SIZE_KEYS: dict[str, str] = {"stl": "stl_bytes"}

#: Backend name -> backend class. Aliases are accepted so both the short and the
#: canonical spelling work in config.
CAD_BACKEND_ALIASES: dict[str, type[CADBackend]] = {
    "openscad": OpenSCADBackend,
    "scad": OpenSCADBackend,
    "mesh": MeshCADBackend,
}

#: Used when neither the argument, the environment nor the runtime setting picks
#: a backend.
DEFAULT_CAD_BACKEND = "openscad"

#: Runtime (database) setting keys tried in order before falling back to the
#: default. The configuration app only knows registered names, so an unknown key
#: simply raises and is skipped.
_RUNTIME_SETTING_KEYS: tuple[str, ...] = ("mesh_backend", "cad_backend")

#: Environment variable / Django setting holding the backend name.
_BACKEND_SETTING = "MESH_BACKEND"


class CADValidationFailed(ValueError):
    """The backend reported blocking problems before export."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def get_storage() -> Any:
    """Return the configured storage backend (indirection point for tests).

    ``files.services`` pulls in the configuration models, so the import happens
    here rather than at module scope: that keeps ``import designs.cad`` working
    before ``django.setup()`` has populated the app registry. Tests replace this
    function to inject a temporary :class:`files.services.LocalStorage`.
    """
    from files.services import get_storage as factory

    return factory()


def _read_runtime_setting(name: str) -> str:
    """Read a runtime-editable backend name, or ``""`` when it is not set.

    The lookup is wrapped exactly like ``files.services``/``slicers.services``
    do: an unregistered key or an unavailable configuration app must not break
    rendering, it just means "not configured".
    """
    try:
        from configuration.services import get_setting
    except Exception:  # noqa: BLE001 - configuration app may be unavailable
        logger.warning("configuration service unavailable; ignoring runtime setting %r", name)
        return ""
    try:
        value = get_setting(name)
    except Exception:  # noqa: BLE001 - unknown key / settings backend hiccup
        logger.debug("get_setting(%r) failed; ignoring it", name, exc_info=True)
        return ""
    return str(value or "")


def resolve_backend_name(name: str | None = None) -> str:
    """Resolve the configured backend name (argument -> env -> runtime -> default).

    ``name`` is the explicit override used by callers and tests. ``MESH_BACKEND``
    (environment variable first, then the Django setting) is the operator-facing
    switch; the runtime settings ``mesh_backend`` / ``cad_backend`` let the config
    app change it without a restart. An unknown name is *not* validated here --
    :func:`get_backend` reports it with the list of supported backends.
    """
    explicit = str(name or "").strip()
    if explicit:
        return explicit.lower()
    env = os.environ.get(_BACKEND_SETTING) or getattr(settings, _BACKEND_SETTING, "")
    from_env = str(env or "").strip()
    if from_env:
        return from_env.lower()
    for key in _RUNTIME_SETTING_KEYS:
        from_runtime = _read_runtime_setting(key).strip()
        if from_runtime:
            return from_runtime.lower()
    return DEFAULT_CAD_BACKEND


def get_backend(name: str | None = None) -> CADBackend:
    """Return the configured CAD backend (indirection point for tests)."""
    resolved = resolve_backend_name(name)
    backend_cls = CAD_BACKEND_ALIASES.get(resolved)
    if backend_cls is None:
        supported = ", ".join(sorted(set(CAD_BACKEND_ALIASES)))
        raise CADError(f"Unknown CAD backend {resolved!r}; expected one of: {supported}")
    return backend_cls()


def _persist_status(
    version: ModelVersion, fields: dict[str, Any], *, update_fields: list[str] | None = None
) -> None:
    current = dict(version.validation_json or {})
    current.update(fields)
    version.validation_json = current
    version.save(update_fields=update_fields or ["validation_json"])


def _build_model(backend: CADBackend, specification: dict[str, Any]) -> GeneratedModel:
    """Run the backend's generate step and wrap the result in a ``GeneratedModel``.

    ``CADBackend.build_model`` is the parametric default; the ``getattr`` keeps
    duck-typed backends (test doubles that only implement the ABC methods)
    working exactly as before.
    """
    builder = getattr(backend, "build_model", None)
    if callable(builder):
        return builder(specification)
    return GeneratedModel(specification=specification, scad_source=backend.generate(specification))


def _artifact_payload(backend: CADBackend, model: GeneratedModel, kind: str) -> bytes:
    """Return the bytes for artifact ``kind``.

    ``scad`` is the already-rendered source (no second render, no shell call);
    every other kind goes through ``backend.export``.
    """
    if kind == "scad":
        return model.scad_source.encode("utf-8")
    return backend.export(model, kind)


def _artifact_path(version: ModelVersion, kind: str) -> str:
    """Relative storage path of artifact ``kind`` for ``version``.

    ``designs.models`` is imported here rather than at module scope so importing
    :mod:`designs.cad` stays possible before ``django.setup()`` has populated the
    app registry.
    """
    from ..models import model_artifact_path

    return model_artifact_path(version, ARTIFACT_FILENAMES[kind])


def render_version(
    version: ModelVersion,
    *,
    backend: CADBackend | None = None,
    storage: Any | None = None,
) -> dict[str, Any]:
    """Generate the artifacts for ``version`` and persist them.

    Writes one file per ``CADBackend.produced_artifacts`` entry through
    :func:`files.services.get_storage` and records the relative paths both on the
    model fields and in ``validation_json``. Returns a small result dict. On
    failure the error is persisted to ``validation_json`` and the exception is
    re-raised so Celery can mark the task failed / retry.
    """
    backend = backend or get_backend()
    storage = storage or get_storage()
    specification = dict(version.specification_json or {})

    _persist_status(
        version,
        {
            "status": "running",
            "stage": "generate",
            "errors": [],
            "started_at": timezone.now().isoformat(),
        },
    )

    try:
        model = _build_model(backend, specification)

        problems = backend.validate(model)
        if problems:
            raise CADValidationFailed(problems)

        _persist_status(version, {"stage": "export"})

        # A mesh backend has no source code to persist, so the manifest string is
        # never written as a ``.scad``; OpenSCAD keeps both artifacts.
        kinds = tuple(getattr(backend, "produced_artifacts", CADBackend.produced_artifacts))
        writes_source = bool(getattr(backend, "produces_source_code", True))
        targets: dict[str, str] = {}
        payloads: dict[str, bytes] = {}
        for kind in kinds:
            if kind not in ARTIFACT_TARGETS:
                raise CADError(
                    f"backend {getattr(backend, 'name', '?')!r} requested unknown "
                    f"artifact kind {kind!r}; expected one of: "
                    f"{', '.join(sorted(ARTIFACT_TARGETS))}"
                )
            if kind == "scad" and not writes_source:
                logger.debug("backend produced no source code; skipping the .scad artifact")
                continue
            payload = _artifact_payload(backend, model, kind)
            relative = _artifact_path(version, kind)
            storage.write_bytes(relative, payload)
            targets[kind] = relative
            payloads[kind] = payload
    except Exception as exc:  # noqa: BLE001 - persist *any* failure, then re-raise
        logger.warning("CAD render failed for ModelVersion %s: %s", version.pk, exc)
        _persist_status(
            version,
            {
                "status": "failed",
                "stage": "failed",
                "errors": [f"{type(exc).__name__}: {exc}"],
                "completed_at": timezone.now().isoformat(),
            },
        )
        raise

    update_fields: list[str] = []
    status_fields: dict[str, Any] = {
        "status": "done",
        "stage": "done",
        "errors": [],
    }
    result_fields: dict[str, Any] = {}
    for kind, relative in targets.items():
        field_name = ARTIFACT_TARGETS[kind][1]
        getattr(version, field_name).name = relative
        update_fields.append(field_name)
        status_fields[f"{kind}_file"] = relative
        result_fields[f"{kind}_file"] = relative
        size_key = ARTIFACT_SIZE_KEYS.get(kind)
        if size_key is not None:
            status_fields[size_key] = len(payloads[kind])
            result_fields[size_key] = len(payloads[kind])
    status_fields["completed_at"] = timezone.now().isoformat()
    _persist_status(version, status_fields, update_fields=[*update_fields, "validation_json"])

    return {
        "version_id": version.pk,
        "status": "done",
        **result_fields,
    }
