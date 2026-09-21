# CAD worker

The CAD worker transforms a `designs.ModelVersion` specification into an STL
using OpenSCAD. It runs **out of process**, behind Celery/Redis, so a long or
hostile model can never block the web worker (terv.md 9., 20. fejezet).

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

### Status (polled by the API)

Progress is persisted in `ModelVersion.validation_json`, so **no new model
fields are required**. The API polls it (plain `fetch` + JSON polling, no HTMX):

```json
{
  "status": "queued | running | done | failed",
  "stage": "generate | export | done | failed",
  "errors": ["RuntimeError: ..."],
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
