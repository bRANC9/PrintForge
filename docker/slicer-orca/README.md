# OrcaSlicer sandbox image

Headless [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer) CLI used by the
optional OrcaSlicer slicing backend to turn a stored STL/3MF into sliced output.
Like the PrusaSlicer image it is **never** run from the Django request cycle: the
slicing task builds one ephemeral container per job.

OrcaSlicer is a wxWidgets/OpenGL application that opens an X display even in CLI
mode, so this image ships `Xvfb` and the entrypoint wraps the real binary in
`xvfb-run -a`. That is the main difference from
[`../slicer`](../slicer) (PrusaSlicer), which runs truly GUI-less.

## Build

```bash
docker build -t printforge-orcaslicer docker/slicer-orca
```

The build downloads the pinned upstream release
`OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.2.AppImage` (currently 2.4.2,
`ORCASLICER_VERSION` build arg), verifies its SHA-256
(`ORCA_APPIMAGE_SHA256`) and extracts it to `/opt/orcaslicer` — no FUSE, so no
`--device /dev/fuse` or `--privileged` is needed. The AppImage is built upstream
on Ubuntu 24.04 (glibc ≥ 2.38), which is why the base image is `ubuntu:24.04`
rather than `debian:bookworm-slim`.

## Run (exact sandbox contract)

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
  printforge-orcaslicer \
  --slice 1 \
  --load-settings "/work/printer.json;/work/process.json" \
  --load-filaments /work/filament.json \
  --outputdir /out \
  /work/model.stl
```

| Flag | Why |
| --- | --- |
| `--network none` | profile data must not reach the network |
| `--read-only` | immutable root filesystem |
| `--tmpfs /tmp` | only writable scratch space (`HOME`, Xvfb socket, Orca config), discarded with the container |
| `--cap-drop ALL` | drop all Linux capabilities |
| `--security-opt no-new-privileges` | no privilege escalation |
| `--pids-limit 256` | fork-bomb protection |
| `--memory 2g --cpus 2.0` | CPU/RAM caps |

`/work` is mounted **read-only** (mesh + generated JSON profiles) and `/out`
read-write (sliced output). A hard timeout (`SLICER_TIMEOUT_SEC`) is applied by
Python around the `docker run` call so a hanging slicer cannot pin the worker.

OrcaSlicer writes a per-run log (`00000.log`) into its **current working
directory**, so the entrypoint `cd`s to the writable `/tmp` tmpfs before
launching; input/output are passed as absolute paths and are unaffected. `/out`
must be writable by the sandbox user — the worker creates the job dirs, so this
holds under the same Docker-out-of-Docker setup as the PrusaSlicer image.

## OrcaSlicer CLI differences from PrusaSlicer

The PrusaSlicer backend passes `--load` with PrusaSlicer `.ini` profiles. The
OrcaSlicer CLI is closer to the BambuStudio lineage and uses **JSON** presets:

| PrusaSlicer | OrcaSlicer |
| --- | --- |
| `--load printer.ini` (once per profile) | `--load-settings "printer.json;process.json"` |
| `--load filament.ini` | `--load-filaments "filament.json"` |
| `--export-gcode --output /out/model.gcode` | `--slice 1 --export-3mf /out/model.gcode.3mf` (or `--outputdir /out`) |
| `--dont-arrange` for a generated plate 3MF | `--arrange 0` to keep plate coordinates |

Notes:

- `--slice 0` slices all plates, `--slice N` a single plate.
- `--export-3mf` produces a `.gcode.3mf` archive (G-code at
  `Metadata/plate_1.gcode`), not a raw `.gcode`; extract it if downstream tooling
  expects plain G-code.
- `--datadir <dir>` moves OrcaSlicer's profile/config directory, useful when the
  worker wants per-job isolation instead of `$HOME/.config/OrcaSlicer`.
- The bundled printer/process/filament presets live under
  `/opt/orcaslicer/resources/profiles/`.

If WebKit/GL runs out of shared memory in a constrained container, add
`--shm-size=512m` to the `docker run` flags.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `SLICER_MODE` | `local` | `local` (host binary) or `docker` (sandbox image) |
| `SLICER_BACKEND` | `prusaslicer` | active backend: `prusaslicer` or `orca` |
| `ORCASLICER_BINARY` | `orca-slicer` | host binary used in `local` mode |
| `ORCASLICER_IMAGE` | `ghcr.io/branc9/printforge-orcaslicer:latest` | image used in `docker` mode |
| `SLICER_DOCKER_BINARY` | `docker` | container runtime binary |
| `SLICER_TIMEOUT_SEC` | `600` | hard timeout for one slice |
| `SLICER_MEMORY_LIMIT` | `2g` | `--memory` value |
| `SLICER_CPU_LIMIT` | `2.0` | `--cpus` value |

The OrcaSlicer backend is **opt-in**: PrusaSlicer stays the default and no Orca
container is launched unless a job selects it (`SLICER_BACKEND=orca`).
