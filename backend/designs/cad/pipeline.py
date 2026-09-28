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

After a successful export the STL is measured **once** from the bytes that were
just written (:func:`dimension_warnings`) and any finding is appended to
``warnings`` -- the same advisory list ``designs.services`` fills with the
meshcheck findings of an imported mesh, so the API and the UI already carry it.
``warnings`` is only created when there is something to say: a clean part keeps
the exact ``validation_json`` shape it had before this check existed.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.utils import timezone

from .base import CADBackend, CADError, GeneratedModel
from .dimensions import dimension_issues
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
    "dimension_warnings",
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

    ``CADBackend.build_model`` is the single entry point: the backend reports
    what it had to skip or synthesise on ``GeneratedModel.warnings``, so this must
    be called exactly once per render. The ``getattr`` keeps duck-typed backends
    (test doubles that only implement the ABC methods) working as before.
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


def dimension_warnings(payload: bytes, dimensions: Mapping[str, Any] | None) -> list[str]:
    """Non-blocking dimension findings for an **already exported** STL.

    ``payload`` is the exact byte string the backend just returned from
    ``export`` (no second export, no storage round-trip, no shell call) and
    ``dimensions`` is the specification's ``dimensions`` mapping -- or ``None`` /
    a partial mapping, in which case only the build-plate check applies.

    This is the byte-level half of :func:`designs.cad.dimensions.dimension_issues`
    and the single reusable entry point for it: :func:`render_version` calls it
    after the export, and the agent path (``agents.tasks._persist_version``, which
    exports through the graph's Validator node instead of through this module)
    calls the same function with its own ``stl_bytes``, so both render paths get
    an identical verdict.

    Never raises and never blocks. ``trimesh`` / ``numpy`` are imported lazily
    (the :mod:`designs.cad.meshcheck` pattern) so a missing dependency or an
    unparseable payload yields no findings instead of failing a render that
    already produced a valid artifact: this is a warning about a *good* mesh, so
    it must not be the reason a job fails.
    """
    if not payload:
        return []
    try:
        import numpy as np

        from .meshcheck import load_mesh

        bounds = np.asarray(load_mesh(bytes(payload), "stl").bounds, dtype=np.float64)
    except Exception:  # noqa: BLE001 - an unmeasurable mesh is not a render failure
        logger.warning("Could not measure the exported STL; skipping the dimension check")
        logger.debug("dimension check detail", exc_info=True)
        return []
    if bounds.shape != (2, 3):  # pragma: no cover - trimesh always returns a 2x3 box
        return []
    (low_x, low_y, low_z), (high_x, high_y, high_z) = bounds
    extents = (float(high_x - low_x), float(high_y - low_y), float(high_z - low_z))
    return dimension_issues(extents, dimensions, min_z_mm=float(low_z))


def _merge_warnings(existing: Any, findings: list[str]) -> list[str]:
    """``validation_json["warnings"]`` after appending ``findings``.

    Appended, never replaced: the key is shared with the mesh-import findings
    ``designs.services`` records (``_persist_validation(..., {"warnings": ...})``)
    and a re-render only rewrites the status/stage keys, so both kinds of finding
    have to survive together. De-duplicated in place because a re-render of the
    same version would otherwise stack identical strings.
    """
    merged = [str(warning) for warning in (existing or [])]
    for finding in findings:
        if finding not in merged:
            merged.append(finding)
    return merged


def _dimension_findings(payload: bytes, specification: Mapping[str, Any]) -> list[str]:
    """:func:`dimension_warnings` for a render, wrapped so it cannot fail the job.

    The check is a *diagnostic on a mesh that already rendered*, so it runs after
    the export and outside the render's ``try``: an exception here must not turn a
    finished render into a failed one, and above all must not leave the version
    stuck in ``status="running"`` for the API to poll forever (terv.md 9.
    fejezet). Anything unexpected is logged and the render finishes clean.
    """
    try:
        dimensions = specification.get("dimensions") if isinstance(specification, Mapping) else None
        return dimension_warnings(payload, dimensions)
    except Exception:  # noqa: BLE001 - a broken check is not a render failure
        logger.warning("Dimension check failed; continuing without it", exc_info=True)
        return []


def render_version(
    version: ModelVersion,
    *,
    backend: CADBackend | None = None,
    storage: Any | None = None,
) -> dict[str, Any]:
    """Generate the artifacts for ``version`` and persist them.

    Writes one file per ``CADBackend.produced_artifacts`` entry through
    :func:`files.services.get_storage` and records the relative paths both on the
    model fields and in ``validation_json``. The exported STL is then measured
    against the requested ``dimensions`` (:func:`dimension_warnings`) and any
    finding is appended to ``validation_json["warnings"]``; the job still finishes
    ``done``, because an envelope mismatch is a warning, not a failure. Returns a
    small result dict. On failure the error is persisted to ``validation_json``
    and the exception is re-raised so Celery can mark the task failed / retry.
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
    # Measured *after* the try/except on purpose: the check runs on a render that
    # already produced a valid STL, so it must never be able to turn a success
    # into a persisted failure (nor leave the version stuck in "running"). Only
    # the bytes export() just returned are read -- no second export.
    findings = [str(w) for w in (getattr(model, "warnings", ()) or ())]
    for item in _dimension_findings(payloads.get("stl", b""), specification):
        if item not in findings:
            findings.append(item)
    if findings:
        status_fields["warnings"] = _merge_warnings(
            (version.validation_json or {}).get("warnings"), findings
        )
    _persist_status(version, status_fields, update_fields=[*update_fields, "validation_json"])

    return {
        "version_id": version.pk,
        "status": "done",
        **result_fields,
    }
