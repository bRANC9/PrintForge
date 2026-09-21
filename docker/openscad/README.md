# OpenSCAD sandbox image

Headless [OpenSCAD](https://openscad.org/) CLI used by the CAD pipeline to turn
generated `.scad` source into STL. It is **never** run from the Django request
cycle: the `designs.render_model_stl` Celery task builds one ephemeral container
per job (terv.md 9., 20. fejezet).

## Build

```bash
docker build -t printforge-openscad docker/openscad
```

## Run (exact sandbox contract)

The worker (`backend/designs/cad/openscad.py`, `OPENSCAD_MODE=docker`) builds
exactly this command as a **list of arguments** (`shell=False`):

```bash
docker run --rm \
  --network none \
  --read-only \
  --tmpfs /tmp \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 256 \
  --memory 1g \
  --cpus 1.0 \
  -v "$JOB_DIR/in":/work:ro \
  -v "$JOB_DIR/out":/out:rw \
  printforge-openscad \
  -o /out/model.stl /work/model.scad
```

| Flag | Why |
| --- | --- |
| `--network none` | generated code must not reach the network |
| `--read-only` | immutable root filesystem |
| `--tmpfs /tmp` | only writable scratch space, discarded with the container |
| `--cap-drop ALL` | drop all Linux capabilities |
| `--security-opt no-new-privileges` | no privilege escalation |
| `--pids-limit 256` | fork-bomb protection |
| `--memory 1g --cpus 1.0` | CPU/RAM caps (configurable via `OPENSCAD_MEMORY_LIMIT` / `OPENSCAD_CPU_LIMIT`) |

`/work` is mounted **read-only** (only holds the generated `.scad`) and `/out`
read-write (only the produced STL). A hard `timeout=OPENSCAD_TIMEOUT_SEC` is
applied by Python around the `docker run` call, so an infinite OpenSCAD loop
cannot pin the worker.

> Note: `--tmpfs /tmp` and `--read-only` are complementary here; the container
> root is read-only while `/tmp` is a writable in-memory mount. `HOME=/tmp` is
> set in the image so OpenSCAD can persist preferences there.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENSCAD_MODE` | `local` | `local` (host binary) or `docker` (this image) |
| `OPENSCAD_IMAGE` | `printforge-openscad` | image used in `docker` mode |
| `OPENSCAD_DOCKER_BINARY` | `docker` | container runtime binary |
| `OPENSCAD_BINARY` | `openscad` | host binary used in `local` mode |
| `OPENSCAD_TIMEOUT_SEC` | `60` | hard timeout for one export |
| `OPENSCAD_MEMORY_LIMIT` | `1g` | `--memory` value |
| `OPENSCAD_CPU_LIMIT` | `1.0` | `--cpus` value |

## Security invariants

* The generated `.scad` is scanned for `import()`, `surface()`, `include <..>`
  and `use <..>` before the CLI runs; any hit aborts the job.
* Specification values are coerced to bounded numbers, so they cannot inject
  OpenSCAD statements.
* No shell is ever involved: arguments are always passed as a list.
