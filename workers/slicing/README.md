# Slicing worker

The slicing worker turns a stored `designs.ModelVersion` STL/3MF into G-code
using PrusaSlicer. It runs **out of process**, behind Celery/Redis, so a long or
hostile profile can never block the web worker (terv.md 12., 20. fejezet).

## Contract

| Item | Value |
| --- | --- |
| Enqueue | `slice_print_job.delay(job_id)` |
| Celery task name | `slicers.slice_print_job` |
| Implementation | `backend/slicers/tasks.py` |
| Backend code | `backend/slicers/prusaslicer.py` |
| Broker / result | `REDIS_URL` (`CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND`) |
| Input | `printers.PrintJob` (model version + printer/filament/process profiles) |
| Output | `PrintJob.gcode` (stored via `files.services.get_storage()`) |
| Estimate | `PrintJob.slicing_json` |
| Printer match | `PrintJob.printer_profile` |
| Status | `PrintJob.status` (`PrintJobStatus`) |

### Input

`job_id: int` — the primary key of a `printers.PrintJob`. The worker reads
`PrintJob.model_version.stl_file`, the physical `PrintJob.printer`, and the
optional `filament` / `slicer_profile` (a `ProcessProfile`).

The slicer printer profile is resolved by name, then `backend`, then
`is_default`, falling back to the physical printer's own name/backend
(`slicers.services.resolve_printer_profile`). The resolved row is persisted to
`PrintJob.printer_profile`, so the match is explicit instead of implicit.

### Status transitions

```text
QUEUED -> PREPARING -> SLICING -> READY
                              \-> FAILED   (exception re-raised for Celery)
```

* `PREPARING` is written (together with the resolved `printer_profile`) before
  the model artifact is read.
* `SLICING` is written right before the CLI runs.
* On success the G-code is stored, `PrintJob.gcode` is set and the estimate is
  persisted into `PrintJob.slicing_json` (`status`, `format`, `gcode`,
  `gcode_bytes`, `slicer`, `estimate`, `computed_at`).
* On any failure `FAILED` and the error are persisted into `slicing_json`, then
  the exception is re-raised so Celery records the failure and can retry.

### Estimate

`slicers.prusaslicer.parse_gcode_estimates` reads the comment headers
PrusaSlicer writes into the G-code:

```text
; estimated printing time (normal mode) = 1h 2m 3s
; filament used [mm] = 1234.56
; filament used [g] = 3.70
```

`SlicerBackend.estimate(result)` normalises them to
`{"estimated_time_sec", "estimated_time", "filament_g", "filament_mm", "format",
"slicer"}`.

## Running

```bash
# from backend/ (or the celery image/command in docker-compose)
uv run celery -A config worker -l info
```

`config/celery.py` autodiscovers `slicers.tasks`, so the task is registered as
`slicers.slice_print_job`.

## Sandbox

PrusaSlicer runs either on the host (``slicer_mode=local``, dev) or in the
locked-down container (``slicer_mode=docker``, production) with:

```text
--network none --read-only --tmpfs /tmp --cap-drop ALL
--security-opt no-new-privileges --pids-limit 256 --memory 2g --cpus 2.0
```

plus a hard ``slicer_timeout_sec`` timeout. Profile `settings_json` is validated
before it reaches the CLI; `subprocess` is always called with a list of
arguments and `shell=False`. See `docker/slicer/README.md`.

### Runtime settings

`slicer_mode` and `slicer_timeout_sec` are resolved **at call time** through
`configuration.services.get_setting` (DB override -> Django settings ->
`os.environ` -> default), so an admin change applies to the next job with no
worker restart and no import-time caching. Defaults: `local` / `300`.

Transport overrides are env-only (read at call time):

| Variable | Default | Meaning |
| --- | --- | --- |
| `PRUSASLICER_BINARY` | `prusa-slicer` | host binary used in `local` mode |
| `SLICER_IMAGE` | `ghcr.io/branc9/printforge-prusaslicer:latest` | container image (wins over the legacy `PRUSASLICER_IMAGE`) |
| `PRUSASLICER_IMAGE` | – | legacy image override |
| `SLICER_DOCKER_BINARY` | `docker` | container runtime binary |
| `SLICER_MEMORY_LIMIT` | `2g` | `--memory` value |
| `SLICER_CPU_LIMIT` | `2.0` | `--cpus` value |

> A later OrcaSlicer backend (wxWidgets/GL, often needs Xvfb) will implement the
> same `SlicerBackend` contract; it is intentionally not implemented yet.
