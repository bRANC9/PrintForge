# CAD worker

The CAD worker transforms a `designs.ModelVersion` specification into an STL.
It runs **out of process**, behind Celery/Redis, so a long or hostile model can
never block the web worker (terv.md 9., 20. fejezet).

Two backends are selectable at runtime through `MESH_BACKEND`:

| `MESH_BACKEND` | Backend | Artifacts |
| --- | --- | --- |
| `""` (default) | `OpenSCADBackend` — parametric OpenSCAD CLI | `model.scad` + `model.stl` |
| `"mesh"` | `MeshCADBackend` — adopts `ModelVersion.source_mesh` | `model.stl` only |

## Contract

| Item | Value |
| --- | --- |
| Enqueue | `render_model_stl.delay(version_id)` |
| Celery task name | `designs.render_model_stl` |
| Implementation | `backend/designs/tasks.py` |
| Backend code | `backend/designs/cad/` |
| Broker / result | `REDIS_URL` (`CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND`) |
| Input | `ModelVersion.specification_json` (plain `dict`) |
| Output | `ModelVersion.scad_file`, `ModelVersion.stl_file` |
| Status | `ModelVersion.validation_json["status"]` |

### Input

`version_id: int` — the primary key of a `designs.ModelVersion`. The worker
reads `specification_json`; it does **not** import `agents.spec` (the CAD
package stays decoupled from `llm-provider`).

The mesh backend additionally reads the source mesh named by
`specification_json["mesh"]["source"]` (a relative storage path, normally
`ModelVersion.source_mesh`) — see `backend/designs/cad/mesh.py`.

### Status (polled by the API)

Progress is persisted in `ModelVersion.validation_json`, so **no new model
fields are required**. The API polls it (plain `fetch` + JSON polling, no HTMX):

```json
{
  "status": "queued | running | done | failed",
  "stage": "generate | export | done | failed",
  "errors": ["RuntimeError: ..."],
  "warnings": ["rendered Y extent 195.0mm differs from the requested height 90.0mm by +117% (tolerance +-25%)"],
  "scad_file": "projects/<id>/v<version>/model.scad",
  "stl_file": "projects/<id>/v<version>/model.stl",
  "stl_bytes": 12345,
  "started_at": "2026-09-21T12:00:00+00:00",
  "completed_at": "2026-09-21T12:00:05+00:00"
}
```

Transitions:

```text
queued  ->  running  ->  done
                     \->  failed   (errors[] populated, exception re-raised)
```

* `queued` is set by the caller/API service right before `delay()`.
* `running` / `stage=generate` is written by the worker when it starts.
* On success `scad_file` / `stl_file` are also written and `stage=done`.
* On any failure the error is persisted first, then the exception is re-raised
  so Celery records the failure and can retry.

`warnings` is **advisory and never blocking**: the job still finishes `done`. It
is only present when there is something to report, and it is shared with the
mesh-import findings `designs.services` records for an uploaded source mesh, so
both kinds of finding survive a re-render. Two sources write to it:

* `designs.cad.pipeline.dimension_warnings` — after the export, the exported STL
  is measured once and compared with the requested `dimensions`
  (`width`/`height`/`thickness`, ±25%, see `designs/cad/dimensions.py`), plus a
  "rests on the build plate" check on the minimum Z. A clean part produces no
  key at all.
* `designs.services.source_mesh_warnings` — the meshcheck findings of an
  imported mesh.

A mesh render produces no source code, so `scad_file` is **absent** from
`validation_json` (and `ModelVersion.scad_file` stays empty); `stl_file` and
`stl_bytes` are written exactly as above. No key is ever renamed or removed.

The API exposes this by returning `version.validation_json` for the version.

## Running

```bash
# from backend/ (or the celery image/command in docker-compose)
uv run celery -A config worker -l info
```

`config/celery.py` autodiscovers `designs.tasks`, so the task is registered as
`designs.render_model_stl`.

## Storage

Artifacts are written through `files.services.get_storage()` (local filesystem
today, MinIO/S3-compatible later). The DB only stores the relative paths
returned by `designs.models.model_artifact_path`:

```text
projects/<project_id>/v<version>/model.scad
projects/<project_id>/v<version>/model.stl
```

## OpenSCAD sandbox

The OpenSCAD CLI runs either on the host (`OPENSCAD_MODE=local`, dev) or in the
locked-down container (`OPENSCAD_MODE=docker`, production) with:

```text
--network none --read-only --tmpfs /tmp --cap-drop ALL
--security-opt no-new-privileges --pids-limit 256 --memory 1g --cpus 1.0
```

plus a hard `OPENSCAD_TIMEOUT_SEC` timeout. `import()` / `surface()` / `include`
/ `use` are rejected before execution; `subprocess` is always called with a list
of arguments and `shell=False`. See `docker/openscad/README.md`.

## Mesh backend

`MeshCADBackend` does **not** shell out, so none of the sandbox flags apply: it
reads the source mesh bytes from storage, parses them with `trimesh` and writes
the transformed mesh back. It is bounded by:

* `MESH_MAX_SOURCE_BYTES` — the source mesh above this size is rejected
  (`MeshCheckError`) before parsing.
* `MESH_REPAIR_ENABLED`, `MESH_TARGET_FACES` — repair is best effort: a step
  whose optional trimesh extra is missing is logged in the manifest as a
  warning instead of failing the job. Decimation is skipped (with a warning)
  when neither `fast-simplification` nor `open3d` is installed.
* `MESH_DEFAULT_SCALE_MM` — target size of the **largest bounding-box edge**,
  in millimetres.

Output is Z-up with `min Z = 0` (resting on the build plate), matching the
parametric backend. Printability of the result is reported by
`designs.cad.meshcheck.check_mesh`; blocking problems fail the job exactly like
an OpenSCAD validation failure.
