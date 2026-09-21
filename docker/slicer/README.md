# PrusaSlicer sandbox image

Headless [PrusaSlicer](https://github.com/prusa3d/PrusaSlicer) CLI used by the
slicing pipeline to turn a stored STL/3MF into G-code. It is **never** run from
the Django request cycle: the `slicers.slice_print_job` Celery task builds one
ephemeral container per job (terv.md 12., 20. fejezet).

## Build

```bash
docker build -t printforge-prusaslicer docker/slicer
```

## Run (exact sandbox contract)

The worker (`backend/slicers/prusaslicer.py`, `SLICER_MODE=docker`) builds
exactly this command as a **list of arguments** (`shell=False`):

```bash
docker run --rm \
  --network none \
  --read-only \
  --tmpfs /tmp \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 256 \
  --memory 2g \
  --cpus 2.0 \
  -v "$JOB_DIR/in":/work:ro \
  -v "$JOB_DIR/out":/out:rw \
  printforge-prusaslicer \
  --export-gcode \
  --output /out/model.gcode \
  --load /work/printer.ini \
  --load /work/filament.ini \
  --load /work/process.ini \
  /work/model.stl
```

| Flag | Why |
| --- | --- |
| `--network none` | profile data must not reach the network |
| `--read-only` | immutable root filesystem |
| `--tmpfs /tmp` | only writable scratch space (plus `HOME=/tmp`), discarded with the container |
| `--cap-drop ALL` | drop all Linux capabilities |
| `--security-opt no-new-privileges` | no privilege escalation |
| `--pids-limit 256` | fork-bomb protection |
| `--memory 2g --cpus 2.0` | CPU/RAM caps (configurable via `SLICER_MEMORY_LIMIT` / `SLICER_CPU_LIMIT`) |

`/work` is mounted **read-only** (holds the mesh and the generated `.ini`
files) and `/out` read-write (only the produced G-code). A hard
`timeout=SLICER_TIMEOUT_SEC` is applied by Python around the `docker run` call
so a hanging slicer cannot pin the worker.

## Profile → CLI mapping

Profiles are **data**, not code: each `settings_json` is a flat mapping of
PrusaSlicer config keys.

| `settings_json` key | Effect |
| --- | --- |
| `config_file` (string or list) | copied into the job dir and passed as `--load <file>` |
| `overrides` (object) | passed as `--key=value` (underscores become hyphens) |
| any other scalar/list | written to `<profile>.ini` and passed as `--load` |

PrusaSlicer is told to load `printer.ini`, then `filament.ini`, then
`process.ini`, so the process profile wins over the filament profile, which
wins over the printer profile. `ProcessProfile.layer_height` is injected as
`layer_height` when no explicit value is present. Keys and values are validated
before use; the command is always a list, never a shell string.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `SLICER_MODE` | `local` | `local` (host binary) or `docker` (this image) |
| `PRUSASLICER_BINARY` | `prusa-slicer` | host binary used in `local` mode |
| `PRUSASLICER_IMAGE` | `printforge-prusaslicer` | image used in `docker` mode |
| `SLICER_DOCKER_BINARY` | `docker` | container runtime binary |
| `SLICER_TIMEOUT_SEC` | `600` | hard timeout for one slice |
| `SLICER_MEMORY_LIMIT` | `2g` | `--memory` value |
| `SLICER_CPU_LIMIT` | `2.0` | `--cpus` value |

## Later backend: OrcaSlicer

[OrcaSlicer](https://github.com/SoftFever/OrcaSlicer) is a planned second
backend. It is **not implemented yet**: OrcaSlicer links wxWidgets/OpenGL and
its headless CLI frequently needs a virtual display (`Xvfb`), so containerising
it is a larger integration risk than PrusaSlicer. When it lands it will
implement the same `SlicerBackend` contract (`slice` / `estimate`) and get its
own image; the worker and task interface stay unchanged.
