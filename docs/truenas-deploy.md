# TrueNAS SCALE 25.10 (Goldeye) deployment runbook

Deploy PrintForge on the NAS by **pulling** the prebuilt image from GitHub
Container Registry. Nothing is built on the NAS; the Watchtower instance that is
already running on the host keeps the app containers up to date.

- Image: `ghcr.io/branc9/printforge:latest`
- Published by `.github/workflows/release.yml` (push to `main`, `v*` tags, or a
  manual `workflow_dispatch`).
- Only the `web` port (`8080`) is published. `db`, `redis` and `ollama` stay on
  the internal Compose network — **never expose `11434`**.

> **Read section 9 before deploying:** the CAD/slicing workers cannot run from
> this image yet. The UI/API work; model generation and slicing will fail.

---

## 1. Pre-flight checklist

Do these before touching the NAS.

**a) The image must have been published by CI.** Either open the GitHub UI
(Actions → **Release**) and confirm a green run on `main`, or:

```bash
gh run list --workflow=release.yml --limit 5
```

**b) Registry access.** The repo is **private**, so the GHCR package is private
by default. The NAS then needs credentials to pull. Pick one:

- **Make the package public** (simplest): GitHub → repo → **Packages** →
  `printforge` → *Package settings* → *Change visibility* → **Public**.
- **Or log in on the NAS** with a GitHub Personal Access Token (classic) that has
  the **`read:packages`** scope:

  ```bash
  echo "<PAT>" | docker login ghcr.io -u <github-user> --password-stdin
  ```

  This covers **both** `docker compose pull` **and Watchtower: Watchtower pulls
  through the Docker daemon, so it reuses these stored credentials. Run the
  login as the same user that runs the Docker daemon (on TrueNAS that is usually
  `root`, so prefix with `sudo` if your shell is not root).

- Verify access before deploying:

  ```bash
  docker manifest inspect ghcr.io/branc9/printforge:latest > /dev/null && echo "registry OK"
  ```

**c) Host requirements:** TrueNAS SCALE 25.10 with the native Docker +
`docker compose` plugin, SSH access, and a pool dataset to hold the data.

## 2. Dataset and `.env`

Persistent data and secrets live on the pool, **never in the repo** (the SCALE
root filesystem is immutable and the repo may be re-cloned).

```bash
mkdir -p /mnt/<pool>/apps/printforge
cd /mnt/<pool>/apps/printforge

# compose files + docker/db/init live in the clone
git clone https://github.com/bRANC9/PrintForge.git repo

# env file stays on the pool
cp repo/.env.example .env
chmod 600 .env
```

Edit `/mnt/<pool>/apps/printforge/.env`. Minimum to change:

```env
DJANGO_DEBUG=false
DJANGO_SECRET_KEY=<paste output of the command below>
DJANGO_ALLOWED_HOSTS=truenas.lan,192.168.1.10
DJANGO_CSRF_TRUSTED_ORIGINS=          # only needed with HTTPS (section 6)
POSTGRES_PASSWORD=<strong-password>
DATA_ROOT=/mnt/<pool>/apps/printforge
WEB_PORT=8080
PRINTFORGE_IMAGE=ghcr.io/branc9/printforge:latest
```

Generate the secret key with:

```bash
python3 -c "import secrets;print(secrets.token_urlsafe(64))"
```

Sanity-check the two critical secrets before starting:

```bash
grep -q '^DJANGO_SECRET_KEY=.\{16,\}' .env && echo "secret key: OK" || echo "secret key: NOT SET"
grep -q '^POSTGRES_PASSWORD=change-me' .env && echo "password: STILL THE PLACEHOLDER" || echo "password: OK"
```

`DATA_ROOT` ends up holding `pg/`, `redis/`, `media/` and (internal Ollama mode)
`ollama/`.

## 3. Validate, then start

```bash
cd /mnt/<pool>/apps/printforge/repo

# validate both variants (no daemon needed)
docker compose --env-file ../.env -f docker-compose.prod.yml config --quiet
docker compose --env-file ../.env \
  -f docker-compose.prod.yml -f docker-compose.ollama.yml config --quiet

# confirm the hardened default made it through
docker compose --env-file ../.env -f docker-compose.prod.yml config | grep DJANGO_DEBUG
# expect:  DJANGO_DEBUG: "false"
```

### External Ollama (separate host / GPU box)

```bash
docker compose --env-file ../.env -f docker-compose.prod.yml up -d
```

Point the app at it with `OLLAMA_BASE_URL=http://<ollama-host>:11434` in `.env`.
If left out, the default is `http://host.docker.internal:11434`.

### Internal Ollama (runs on this NAS)

For an NVIDIA GPU, first uncomment the `deploy:` block in
`docker-compose.ollama.yml` and ensure the NVIDIA container toolkit is
configured for Docker. Then:

```bash
docker compose --env-file ../.env \
  -f docker-compose.prod.yml -f docker-compose.ollama.yml up -d
```

The override adds the `ollama` service and forces `OLLAMA_BASE_URL` to
`http://ollama:11434` for `web` and `worker`.

> The `vector` extension is created automatically on the first database init by
> `docker/db/init/01-extensions.sql` (only when the `pg/` dataset is empty).

## 4. Create the admin user

```bash
docker compose --env-file ../.env -f docker-compose.prod.yml \
  exec web python manage.py createsuperuser
```

## 5. Verify the deployment

```bash
cd /mnt/<pool>/apps/printforge/repo

# all services healthy / running
docker compose --env-file ../.env -f docker-compose.prod.yml ps

# API health endpoint
curl -fsS http://<truenas-host>:8080/api/v1/health/
# expect: {"status": "ok"}

# login page renders (templates + static wiring)
curl -fsS http://<truenas-host>:8080/ | head

# follow startup logs if something looks wrong
docker compose --env-file ../.env -f docker-compose.prod.yml logs -f web worker
```

Then in a browser: open `http://<truenas-host>:8080`, log in with the superuser,
and open a couple of pages (home, projects) to confirm the UI renders.

## 6. Reverse proxy / HTTPS variant

Terminate TLS at a reverse proxy (Caddy / Nginx / Traefik) or a Cloudflare
Tunnel, forwarding to `web:8000` (or `http://<truenas-host>:8080`). The proxy
must send `X-Forwarded-Proto: https` and the correct `Host` header.

Then set in `.env`:

```env
DJANGO_ALLOWED_HOSTS=printforge.example.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://printforge.example.com

DJANGO_SECURE_SSL_REDIRECT=true
DJANGO_SESSION_COOKIE_SECURE=true
DJANGO_CSRF_COOKIE_SECURE=true
DJANGO_SECURE_PROXY_SSL_HEADER=true
DJANGO_SECURE_HSTS_SECONDS=3600        # raise to 31536000 once confirmed
```

Why each one:

| Variable | Effect | Why it matters |
| --- | --- | --- |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Trusts `https://<domain>` for unsafe requests | Without it, form POSTs/logins fail behind the proxy |
| `DJANGO_SECURE_SSL_REDIRECT` | Redirects all http → https | Prevents plaintext access |
| `DJANGO_SECURE_PROXY_SSL_HEADER` | Trusts `X-Forwarded-Proto` | Without it Django sees http and the redirect loops |
| `DJANGO_SESSION_COOKIE_SECURE` | Session cookie only over https | Stops cookie leakage on http |
| `DJANGO_CSRF_COOKIE_SECURE` | CSRF cookie only over https | Same, for the CSRF token |
| `DJANGO_SECURE_HSTS_SECONDS` | HSTS max-age | Forces browsers to https; start small, then raise |

Apply by recreating the app containers:

```bash
docker compose --env-file ../.env -f docker-compose.prod.yml up -d
```

> Enable the HTTPS flags **only** when TLS really terminates in front. Turning
> on `DJANGO_SECURE_SSL_REDIRECT` without `DJANGO_SECURE_PROXY_SSL_HEADER` (or
> without a proxy at all) causes a redirect loop and breaks login.

## 7. Watchtower behaviour

Watchtower is already running on the host — **do not add a Watchtower service**.
`web` and `worker` carry `com.centurylinklabs.watchtower.enable: "true"`.

- **Poll interval:** Watchtower's default is 24 h. Check the running container's
  interval with `docker inspect <watchtower> | grep -i interval`.
- If it runs with `--label-enable`, only these labelled containers are updated
  (intended). Without it, all containers are monitored and the label is harmless.
- **Force an immediate update** instead of waiting for the next poll:

  ```bash
  docker ps --format '{{.Names}}' | grep -i watchtower
  docker exec <watchtower-container> /watchtower --run-once --cleanup
  ```

- Watchtower only recreates the app containers; it never touches the DB data.
- **Private package:** Watchtower uses the daemon's stored `docker login`
  credentials. If the PAT expires or is revoked, updates silently stop —
  re-run the `docker login` from section 1b.

## 8. Rollback

Pin `PRINTFORGE_IMAGE` to an immutable build tag, then recreate:

```bash
# every build publishes sha-<short> plus semver tags (v1.2.3 -> 1.2.3 / 1.2)
sed -i 's#^PRINTFORGE_IMAGE=.*#PRINTFORGE_IMAGE=ghcr.io/branc9/printforge:sha-1a2b3c4#' .env
docker compose --env-file ../.env -f docker-compose.prod.yml up -d
```

Because the compose file sets `pull_policy: always`, `up -d` pulls the pinned
tag. Watchtower follows the image reference, so a **pinned `sha-` tag is not
auto-updated** — set `PRINTFORGE_IMAGE` back to `...:latest` to resume automatic
updates. Available tags are listed on the GitHub **Packages** page
(`https://github.com/bRANC9/PrintForge/pkgs/container/printforge`).

## 9. Known limitation: CAD / slicing workers cannot run yet

The published image is the **Django app only** (web/worker). It does **not**
contain OpenSCAD or PrusaSlicer, and the sandbox images
(`docker/openscad/**`, `docker/slicer/**`) are **not published to GHCR** yet.

Consequences on this deployment:

- Working: auth, projects/workspaces, the UI, the API, agent orchestration, RAG
  (with Ollama reachable).
- **Not working:** prompt → SCAD → STL model generation, mesh validation and
  slicing. These calls will fail and surface as a failed job / error
  notification in the UI.

This is expected for the current release; the CAD and slicing workers will get
their own image builds and Compose services in a later phase. Do not report the
model-generation/slicing failures as a deployment bug.

## Notes

- Multi-arch is **not** enabled; the image is `linux/amd64` only, matching
  TrueNAS SCALE on x86-64.
- `db` and `redis` use the same pinned images as the base stack
  (`pgvector/pgvector:pg16`, `redis:7-alpine`).
- Never publish `11434`; keep Ollama on the internal Compose network.
