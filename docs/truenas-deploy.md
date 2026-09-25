# TrueNAS SCALE 25.10 (Goldeye) deployment runbook

Deploy PrintForge by **pulling** the prebuilt image from GitHub Container
Registry. Nothing is built on the NAS; the Watchtower instance already running
on the host keeps the app containers up to date.

- Image: `ghcr.io/branc9/printforge:latest`
- Published by `.github/workflows/release.yml` (push to `main`, `v*` tags, or a
  manual `workflow_dispatch`).
- Only the `web` port (`8080`) is published. `db`, `redis` and `ollama` stay on
  the internal Compose network — **never expose `11434`**.

There are two supported ways to deploy:

| Path | When | File |
| --- | --- | --- |
| **A — Custom App UI (primary)** | You want to paste a YAML in the TrueNAS web UI | [`docker-compose.truenas.yml`](../docker-compose.truenas.yml) |
| **B — SSH + `docker compose` (alternative)** | You prefer the CLI and an `.env` file | [`docker-compose.prod.yml`](../docker-compose.prod.yml) |

> **Section 9 covers the CAD/slicing sandbox** (OpenSCAD + PrusaSlicer images,
> the Docker socket and its security trade-off). Read it before relying on model
> generation or slicing.

---

## 0. Pre-flight (both paths)

**a) The image must have been published by CI.** Open the GitHub UI
(Actions → **Release**) and confirm a green run on `main`, or:

```bash
gh run list --workflow=release.yml --limit 5
```

**b) Registry access.** The repo is **private**, so the GHCR package is private
by default and the NAS needs credentials to pull. Pick one:

- **Make the package public** (simplest): GitHub → repo → **Packages** →
  `printforge` → *Package settings* → *Change visibility* → **Public**.
- **Or log in on the NAS** with a GitHub Personal Access Token (classic) that
  has the **`read:packages`** scope:

  ```bash
  echo "<PAT>" | docker login ghcr.io -u <github-user> --password-stdin
  ```

  This covers **both** `docker compose pull` **and Watchtower: Watchtower pulls
  through the Docker daemon, so it reuses these stored credentials. Run the
  login as the same user that runs the Docker daemon (on TrueNAS that is usually
  `root`, so prefix with `sudo` if your shell is not root).

  > **Custom App UI note:** the UI's install form also accepts registry
  > credentials. If you cannot use SSH, make the package public instead.

- Verify access (from SSH):

  ```bash
  docker manifest inspect ghcr.io/branc9/printforge:latest > /dev/null && echo "registry OK"
  ```

**c) Host requirements:** TrueNAS SCALE 25.10, a pool dataset for the data, and
(for Path B) SSH access.

---

## Path A — TrueNAS Custom App UI (paste YAML) — primary

`docker-compose.truenas.yml` is self-contained: literal values only, no image
build, no env-file, no relative mounts and no variable interpolation, so the UI
can consume it as-is. The `vector` extension is created by the Django migration
(`backend/embeddings/migrations/0001_enable_vector_extension.py`), so **no DB
init SQL mount is needed**.

### A1. Create the datasets

Use the UI (Datasets → Add Dataset) or SSH:

```bash
mkdir -p /mnt/<POOL>/apps/printforge/{pg,redis,media,scratch}
```

### A2. Open the Custom App form and paste the YAML

In the TrueNAS UI: **Apps → Discover Apps → Custom App** (Install via YAML).
Name the app `printforge`, then paste the contents of
[`docker-compose.truenas.yml`](../docker-compose.truenas.yml).

### A3. Edit the marked values

The YAML uses plain anchors so each value is written **once** (`db` holds the DB
credentials via `&db-env`; `web` holds the shared app env via `&app-env`, which
`worker` inherits). Only these `# EDIT:` values need changing:

| Value | Where | What to set |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | `db` | A strong password (web/worker inherit it) |
| `DJANGO_SECRET_KEY` | `web` | `python3 -c "import secrets;print(secrets.token_urlsafe(64))"` |
| `DJANGO_ALLOWED_HOSTS` | `web` | Every hostname/IP you open in the browser **and** your NAS hostname / IP, comma-separated (e.g. `printforge.lan,192.168.1.250,localhost`) |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `web` | The browser **origin** including scheme and port, e.g. `http://192.168.1.250:8080` (required for logins/actions from a non-`localhost` origin) |
| `OLLAMA_BASE_URL` | `web` | Ollama host. Default reaches Ollama on the Docker host; change it if Ollama runs elsewhere (`http://ollama:11434` for internal Ollama) |
| `OLLAMA_VISION_MODEL` | `web` | **Optional.** A vision-capable model (e.g. `llava:7b`, `qwen2.5-vl:7b`) used only by the agent self-check review. Leave empty to reuse `OLLAMA_MODEL` |
| `/mnt/<POOL>/...` | `pg`, `redis`, `media`, `scratch` | Your pool dataset paths |
| `"8080:8000"` | `web` | A different host port if 8080 is taken (optional) |

`DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`,
`OLLAMA_BASE_URL` and `OLLAMA_VISION_MODEL` are edited in the `web` block only —
`worker` inherits them through the anchor (the LLM calls run in the Celery worker).

> **Allowed hosts vs. CSRF origin.** `DJANGO_ALLOWED_HOSTS` takes **hostnames
> and IPs only** (no port, no scheme). `DJANGO_CSRF_TRUSTED_ORIGINS` takes full
> **origins** (`http://host:port`) and is what makes POSTs work when you open the
> app by IP/port instead of `localhost`. Missing the first gives `400
> DisallowedHost` on API calls (the page loads but lists nothing); missing the
> second makes login/actions fail with a CSRF error.

### A4. Storage mapping in the UI

After pasting, check the app's **Storage** section shows the host-path mounts. If
the UI asks you to add them, use these container paths:

| Host path (your pool) | Container path |
| --- | --- |
| `/mnt/<POOL>/apps/printforge/pg` | `/var/lib/postgresql/data` |
| `/mnt/<POOL>/apps/printforge/redis` | `/data` |
| `/mnt/<POOL>/apps/printforge/media` | `/data/media` |
| `/mnt/<POOL>/apps/printforge/scratch` | `/mnt/<POOL>/apps/printforge/scratch` (identical path) |

The `scratch` mount must use the **identical absolute path** on both sides (that
is what lets the sandbox, launched via the Docker socket, see the worker's job
dirs). If the database fails to initialise on first start, set the `pg` dataset
owner to the postgres user (UID/GID `999`) so the container can write to it.

### A5. Install, set the sandbox modes, create the admin user

Click **Install**. The entrypoint waits for the database and runs migrations on
start.

Then open the app's **Settings** page once and set:

- **CAD mód = docker**
- **Slicing mód = docker**

This switches the CAD/slicing workers to the published sandbox images — no env
change or restart needed. Ollama is already pointed at the Docker host via
`OLLAMA_BASE_URL` (A3); change it there if Ollama runs elsewhere.

Finally create the superuser:

- **UI:** open the app → `web` container → three-dot menu → **Shell**, then run:

  ```bash
  python manage.py createsuperuser
  ```

- **Or SSH** (find the container name with `docker ps`):

  ```bash
  docker exec -it ix-printforge-web-1 python manage.py createsuperuser
  ```

### A6. Verify

```bash
docker ps
curl -fsS http://<truenas-host>:8080/api/v1/health/
# {"status": "ok", "git_sha": "<a buildból sütött commit>"}
```

A `git_sha` a pontosan futó build commitja (a release workflow sütötte be a
képbe), a UI alján megjelenő `build: <rövid sha>` pedig ugyanez. Így shell
hozzáférés nélkül is azonosítható a deploy.

Then open `http://<truenas-host>:8080`, log in, and open a couple of pages to
confirm the UI renders. `docker logs` / the app's **Logs** pane shows startup
errors.

> **UI differences to remember:** no `env_file` (all env is inline in the YAML),
> no relative bind mounts (absolute host paths only), and no `${VAR:-default}`
> interpolation (literal values only). Do not add a `Watchtower` service — the
> host one already picks up new `:latest` images via the labels in the YAML.

#### Symptom: the project list is empty even though pages load

The `/projects/` page renders (it is a public template) but the cards are filled
client-side from `/api/v1/projects/`. If that call is rejected, the grid stays
empty and no card is clickable. Check the browser DevTools → **Network** tab and
look for the API request:

| Response | Cause | Fix |
| --- | --- | --- |
| `400 DisallowedHost` | The browser origin's IP/hostname is missing from `DJANGO_ALLOWED_HOSTS` | Add it (A3) and recreate `web` |
| `403 Forbidden` | You are not logged in, or your user is not a member of any workspace | Log in; ensure the user has a `WorkspaceMember` row |

To confirm from inside the container, compare the two calls:

```bash
# 200 -> Django is fine, the issue is the browser's Host header
docker exec -it <web-container> \
  python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/api/v1/projects/').status)"

# 400 -> the IP is not in DJANGO_ALLOWED_HOSTS
curl -si http://<truenas-host>:8080/api/v1/projects/ | head -1
```

**Changing env values requires recreating the container** — Compose applies
`environment:` only at container creation, so `restart` is not enough:

```bash
# Path B
docker compose --env-file ../.env -f docker-compose.prod.yml up -d --force-recreate web
# Path A: Stop then Start the app in the TrueNAS UI (an Update/Redeploy may not recreate it)
docker exec <web-container> env | grep DJANGO_ALLOWED_HOSTS   # confirm it stuck
```

> **Static assets after a deploy.** The app uses WhiteNoise with a
> content-hashed manifest (`app.<hash>.js`), so a new image emits new asset URLs
> and browsers fetch them automatically — no hard refresh needed. Hashed names
> are only produced when `DJANGO_DEBUG=false` (in `DEBUG` the plain filename is
> used on purpose). If a page keeps serving stale JS/CSS, check that
> `DJANGO_DEBUG=false` is set and that `collectstatic` ran (it does on every
> container start).

---

## Path B — SSH + `docker compose` (alternative)

Uses [`docker-compose.prod.yml`](../docker-compose.prod.yml) with an `.env` file
and variable defaults.

### B1. Dataset and `.env`

Persistent data and secrets live on the pool, **never in the repo** (the SCALE
root filesystem is immutable).

```bash
mkdir -p /mnt/<pool>/apps/printforge/{pg,redis,media,scratch}
cd /mnt/<pool>/apps/printforge

# compose files live in the clone
git clone https://github.com/bRANC9/PrintForge.git repo

# env file stays on the pool
cp repo/.env.example .env
chmod 600 .env
```

Edit `/mnt/<pool>/apps/printforge/.env`:

```env
DJANGO_DEBUG=false
DJANGO_SECRET_KEY=<paste output of the command below>
DJANGO_ALLOWED_HOSTS=truenas.lan,192.168.1.10,localhost
DJANGO_CSRF_TRUSTED_ORIGINS=http://192.168.1.10:8080   # origin incl. port; https://<domain> behind TLS (see section 6)
POSTGRES_PASSWORD=<strong-password>
DATA_ROOT=/mnt/<pool>/apps/printforge
SANDBOX_WORK_DIR=/mnt/<pool>/apps/printforge/scratch
WEB_PORT=8080
PRINTFORGE_IMAGE=ghcr.io/branc9/printforge:latest
```

```bash
python3 -c "import secrets;print(secrets.token_urlsafe(64))"

grep -q '^DJANGO_SECRET_KEY=.\{16,\}' .env && echo "secret key: OK" || echo "secret key: NOT SET"
grep -q '^POSTGRES_PASSWORD=change-me' .env && echo "password: STILL THE PLACEHOLDER" || echo "password: OK"
```

### B2. Validate, then start

```bash
cd /mnt/<pool>/apps/printforge/repo

docker compose --env-file ../.env -f docker-compose.prod.yml config --quiet
docker compose --env-file ../.env \
  -f docker-compose.prod.yml -f docker-compose.ollama.yml config --quiet

docker compose --env-file ../.env -f docker-compose.prod.yml config | grep DJANGO_DEBUG
# expect:  DJANGO_DEBUG: "false"
```

**External Ollama** (separate host / GPU box):

```bash
docker compose --env-file ../.env -f docker-compose.prod.yml up -d
```

Set `OLLAMA_BASE_URL=http://<ollama-host>:11434` in `.env` (default is
`http://host.docker.internal:11434`).

**Internal Ollama** (runs on this NAS): for an NVIDIA GPU, uncomment the
`deploy:` block in `docker-compose.ollama.yml` and ensure the NVIDIA container
toolkit is configured for Docker, then:

```bash
docker compose --env-file ../.env \
  -f docker-compose.prod.yml -f docker-compose.ollama.yml up -d
```

The override adds the `ollama` service and forces `OLLAMA_BASE_URL` to
`http://ollama:11434` for `web` and `worker`.

### B3. Create the admin user

```bash
docker compose --env-file ../.env -f docker-compose.prod.yml \
  exec web python manage.py createsuperuser
```

### B4. Verify

```bash
docker compose --env-file ../.env -f docker-compose.prod.yml ps
curl -fsS http://<truenas-host>:8080/api/v1/health/
curl -fsS http://<truenas-host>:8080/ | head
docker compose --env-file ../.env -f docker-compose.prod.yml logs -f web worker
```

---

## 6. Reverse proxy / HTTPS (both paths)

Terminate TLS at a reverse proxy (Caddy / Nginx / Traefik) or a Cloudflare
Tunnel, forwarding to `web:8000` (or `http://<truenas-host>:8080`). The proxy
must send `X-Forwarded-Proto: https` and the correct `Host` header.

Add these to the `web` environment (in the Custom App YAML `worker` inherits
them through the `&app-env` anchor; in Path B put them in `.env`):

```env
DJANGO_ALLOWED_HOSTS=printforge.example.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://printforge.example.com

DJANGO_SECURE_SSL_REDIRECT=true
DJANGO_SESSION_COOKIE_SECURE=true
DJANGO_CSRF_COOKIE_SECURE=true
DJANGO_SECURE_PROXY_SSL_HEADER=true
DJANGO_SECURE_HSTS_SECONDS=3600        # raise to 31536000 once confirmed
```

- **Path A:** add them to the `environment:` block of `web` in the pasted YAML,
  then save/update the app.
- **Path B:** add them to `.env`, then
  `docker compose --env-file ../.env -f docker-compose.prod.yml up -d`.

| Variable | Effect | Why it matters |
| --- | --- | --- |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Trusts `https://<domain>` for unsafe requests | Without it, form POSTs/logins fail behind the proxy |
| `DJANGO_SECURE_SSL_REDIRECT` | Redirects all http → https | Prevents plaintext access |
| `DJANGO_SECURE_PROXY_SSL_HEADER` | Trusts `X-Forwarded-Proto` | Without it Django sees http and the redirect loops |
| `DJANGO_SESSION_COOKIE_SECURE` | Session cookie only over https | Stops cookie leakage on http |
| `DJANGO_CSRF_COOKIE_SECURE` | CSRF cookie only over https | Same, for the CSRF token |
| `DJANGO_SECURE_HSTS_SECONDS` | HSTS max-age | Forces browsers to https; start small, then raise |

> Enable the HTTPS flags **only** when TLS really terminates in front. Turning
> on `DJANGO_SECURE_SSL_REDIRECT` without `DJANGO_SECURE_PROXY_SSL_HEADER` (or
> without a proxy at all) causes a redirect loop and breaks login.

## 7. Watchtower behaviour (both paths)

Watchtower is already running on the host — **do not add a Watchtower service**.
`web` and `worker` carry `com.centurylinklabs.watchtower.enable: "true"` in both
the TrueNAS YAML and the prod compose file.

- **Poll interval:** Watchtower's default is 24 h. Check with
  `docker inspect <watchtower> | grep -i interval`.
- If it runs with `--label-enable`, only these labelled containers are updated
  (intended). Without it, all containers are monitored and the label is harmless.
- **Force an immediate update:**

  ```bash
  docker ps --format '{{.Names}}' | grep -i watchtower
  docker exec <watchtower-container> /watchtower --run-once --cleanup
  ```

- Watchtower only recreates the app containers; it never touches the DB data.
- **Private package:** Watchtower uses the daemon's stored `docker login`
  credentials. If the PAT expires or is revoked, updates silently stop — re-run
  the `docker login` from section 0b.

## 8. Rollback (both paths)

Every build publishes `sha-<short>` plus semver tags (`v1.2.3` → `1.2.3` / `1.2`).
Pin the image reference to an immutable tag:

- **Path A:** edit the `image:` of `web` and `worker` in the YAML to
  `ghcr.io/branc9/printforge:sha-1a2b3c4`, then save/update the app.
- **Path B:**

  ```bash
  sed -i 's#^PRINTFORGE_IMAGE=.*#PRINTFORGE_IMAGE=ghcr.io/branc9/printforge:sha-1a2b3c4#' .env
  docker compose --env-file ../.env -f docker-compose.prod.yml up -d
  ```

Watchtower follows the image reference, so a **pinned `sha-` tag is not
auto-updated** — switch back to `...:latest` to resume automatic updates.
Available tags are listed at
`https://github.com/bRANC9/PrintForge/pkgs/container/printforge`.

## 9. CAD / slicing sandbox (OpenSCAD + PrusaSlicer)

Model generation and slicing run as **ephemeral sandbox containers**, never in
the worker process. For each job the worker shells out to `docker run`
(Docker-out-of-Docker) with a locked-down command:

```text
docker run --rm --network none --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 256 \
  --memory 1g --cpus 1.0 \
  -v <job>/in:/work:ro -v <job>/out:/out:rw \
  ghcr.io/branc9/printforge-openscad:latest -o /out/model.stl /work/model.scad
```

| Flag | Why |
| --- | --- |
| `--network none` | generated code must not reach the network |
| `--read-only` + `--tmpfs /tmp` | immutable rootfs; only in-memory scratch |
| `--cap-drop ALL` + `no-new-privileges` | no capabilities, no escalation |
| `--pids-limit` / `--memory` / `--cpus` | fork-bomb and resource caps |
| Python `timeout` | hard `OPENSCAD_TIMEOUT_SEC` / `SLICER_TIMEOUT_SEC` |

The same flags apply to PrusaSlicer (`docker/slicer`, `--memory 2g --cpus 2.0`).

### Images

| Image | Built from | Purpose |
| --- | --- | --- |
| `ghcr.io/branc9/printforge-openscad:latest` | `docker/openscad/` | `.scad` → `.stl` |
| `ghcr.io/branc9/printforge-prusaslicer:latest` | `docker/slicer/` | `.stl` → `.gcode` |

They are **pulled automatically on first use** by the Docker daemon, so the very
first model/slice may take longer. Pre-pull to avoid the wait:

```bash
docker pull ghcr.io/branc9/printforge-openscad:latest
docker pull ghcr.io/branc9/printforge-prusaslicer:latest
```

The sandbox images are **independent of the app image**: Watchtower only tracks
the `web`/`worker` app containers and does **not** update them. They are rebuilt
and pushed by `release.yml` on every push/tag, and re-pulled by the daemon when
the local copy is missing.

### Docker socket (security trade-off)

The `worker` service mounts `/var/run/docker.sock`. That is **required** for
Docker-out-of-Docker and is the main security trade-off of this deployment:
it grants the container control of the host Docker daemon, so any process in it
can start/stop/inspect **any** container on the host. The socket is mounted into
`worker` only, never into `web`.

### Verify

```bash
# images present (after the manual pull above, or after the first job)
docker images | grep printforge-

# start a model generation in the UI, then watch the sandbox container
docker ps -a --filter ancestor=ghcr.io/branc9/printforge-openscad:latest
```

### How it is wired (Docker CLI + scratch dir + socket)

Everything the sandbox needs is baked into the images and the compose files:

- **Docker CLI** — `docker/django/Dockerfile` copies the client binary from
  `docker:27-cli` into the app image, so `worker` can call `docker run`. Only the
  client is added; the Docker engine is **not** installed (the host daemon is
  reached through the mounted socket).
- **Scratch dir** — the worker's `TMPDIR` points at a scratch directory that is
  bind-mounted at the **identical absolute path** (host == container). This is
  what makes Docker-out-of-Docker work: the host daemon resolves the sandbox
  `-v` source paths on the host, so the worker's job dirs must exist at the same
  path on the host.
  - **Path A (Custom App):** `TMPDIR` and the identical-path volume are already
    in the `worker` block of the YAML (both marked `# EDIT:`).
  - **Path B (SSH):** set `SANDBOX_WORK_DIR=/mnt/<POOL>/apps/printforge/scratch`
    in `.env`; the compose file mounts it and sets `TMPDIR` from it.
- **Docker socket** — mounted into `worker` only (see the security note above).
- **Ollama URL** — `web` and `worker` get
  `OLLAMA_BASE_URL=http://host.docker.internal:11434` (through the anchor) plus
  `extra_hosts: host.docker.internal:host-gateway`, so the LLM calls made by the
  Celery worker reach Ollama running on the Docker host. Change the value if
  Ollama runs on another machine, or to `http://ollama:11434` for internal Ollama.
- **Ollama models** — the main model is `OLLAMA_MODEL` (default
  `qwen3-coder:30b`). **Optional:** set `OLLAMA_VISION_MODEL` to a vision-capable
  model (e.g. `llava:7b`, `qwen2.5-vl:7b`) to run the agent self-check review on
  a separate model; leave it **empty** to reuse `OLLAMA_MODEL`. On Path B
  (SSH/`.env`) set both in `.env`; on Path A (Custom App) edit them in the `web`
  environment of the pasted YAML.
- **Mode switch** — the backend defaults to `local`. Set **CAD mód = docker** and
  **Slicing mód = docker** on the app's Settings page after install (see A5). The
  sandbox image names already default to the published GHCR images, so no env is
  needed for them.

If a model/slice fails, check in this order:

```bash
# 1. CLI present in the worker
docker exec <worker-container> docker --version

# 2. scratch dir exists on the host and is writable
ls -ld /mnt/<POOL>/apps/printforge/scratch

# 3. socket mounted
docker inspect <worker-container> --format '{{json .Mounts}}' | grep docker.sock
```

## 10. If gunicorn workers time out

Symptom (from the `web` container logs):

```text
[CRITICAL] WORKER TIMEOUT (pid:...)
Error handling request (no URI read)
Worker (pid:...) was sent SIGKILL! Perhaps out of memory?
```

**What it means.** The worker was blocked in `recv()` waiting for bytes on a
connection that never sent a request (an idle / keep-alive / health-probe
connection) and hit gunicorn's `--timeout`; gunicorn then kills the stuck worker
and restarts it. The *"Perhaps out of memory?"* text is gunicorn's **generic**
message after a timeout — it does **not** by itself prove OOM. The defaults now
use the `gthread` worker class with `--timeout 60` and `--keep-alive 5`, which
tolerates this pattern.

**Confirm whether it is a real OOM** (memory pressure, not idle connections):

```bash
# did the kernel kill it for memory?
docker inspect <web-container> --format '{{.State.OOMKilled}}'   # true = real OOM
docker inspect <web-container> --format '{{.State.ExitCode}}'    # 137 = SIGKILL

# live memory of each container / the host
docker stats --no-stream
free -h
dmesg | grep -i 'oom\|killed process' | tail
```

**Knobs to turn** (read by the entrypoint, no rebuild needed):

| Variable | Default | When to change |
| --- | --- | --- |
| `GUNICORN_WORKERS` | `2` | Lower to `1` on a small NAS — each worker is a full Django process |
| `GUNICORN_THREADS` | `4` | Concurrency per worker; raise before adding workers |
| `GUNICORN_TIMEOUT` | `60` | Raise (e.g. `120`) if a real request is slow; keep it below the reverse-proxy timeout |
| `GUNICORN_GRACEFUL_TIMEOUT` | `30` | Time allowed for a worker to finish on reload |
| `GUNICORN_KEEPALIVE` | `5` | Seconds an idle keep-alive connection is held |
| `GUNICORN_MAX_REQUESTS` / `_JITTER` | `1000` / `100` | Recycle workers to bound slow leaks |
| `GUNICORN_ACCESS_LOG` | `false` | Set `true` to log every request — makes the next incident diagnosable |
| `GUNICORN_WORKER_CLASS` | `gthread` | Leave as `gthread`; `sync` is what caused the timeouts |
| `GUNICORN_LOG_LEVEL` | `info` | `debug` for more detail |

**Apply:**

- **Path A (Custom App):** the YAML no longer sets `GUNICORN_*` (the entrypoint
  defaults apply). To override one, add it to the `web` environment in the pasted
  YAML (worker inherits it through the anchor), then save/update the app.
- **Path B (SSH):** edit them in `.env`, then
  `docker compose --env-file ../.env -f docker-compose.prod.yml up -d`.

> If you run a reverse proxy or uptime monitor, make sure its read timeout is
> **larger** than `GUNICORN_TIMEOUT`, otherwise the proxy drops slow requests
> even while gunicorn is still working on them.

## Notes

- Multi-arch is **not** enabled; the image is `linux/amd64` only, matching
  TrueNAS SCALE on x86-64.
- `db` and `redis` use the same pinned images as the base stack
  (`pgvector/pgvector:pg16`, `redis:7-alpine`).
- The `vector` pgvector extension is created by the Django migration, so no DB
  init SQL mount is required on TrueNAS (the repo/local stacks keep
  `docker/db/init` for first-init convenience).
- Never publish `11434`; keep Ollama on the internal Compose network.
