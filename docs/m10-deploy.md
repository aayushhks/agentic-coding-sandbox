# M10 — Docs & Deploy

The capstone: package the whole system as one deployable unit and finish the documentation.
Everything built across M1–M9 — the agent, the sandbox, the benchmark, the eval harness, the
dashboard, and the regression gate — ships as a single container image that serves the API and
the dashboard from one process.

## One image, one process

A multi-stage [`Dockerfile`](../Dockerfile):

1. **`frontend` stage** (`node:22`) runs `npm ci && npm run build`, producing the static SPA in
   `/frontend/dist`.
2. **`runtime` stage** (`ghcr.io/astral-sh/uv:python3.13`) installs only production dependencies
   (`uv sync --frozen --no-dev` — no pytest/ruff/mypy/matplotlib), copies the backend source,
   **seeds a read-only SQLite database** from the committed results, and copies the built SPA
   from the first stage into `/app/frontend/dist`.

At runtime FastAPI serves `/health` and `/api/*`, and mounts the built SPA at `/`. The explicit
routes are registered before the static mount, so the API always wins over the catch-all. The
layout matters: `app/main.py` resolves the SPA at `../frontend/dist` relative to the backend, so
the image keeps the repo's `repo/backend` + `repo/frontend/dist` shape under `/app`.

```
/app
├── backend/            # source + .venv (production deps only)
│   └── app/main.py     # serves /api and mounts ../frontend/dist at /
└── frontend/dist/      # built SPA, copied from the node stage
```

### Self-contained by default

The dashboard is **read-only over fixed historical runs** (you can't run the agent in a managed
container — the sandbox needs `unshare --net` privileges hosts don't grant), so the image bakes
the committed v1 and v2 runs into a SQLite database at build time and defaults `DATABASE_URL` to
it. That means a bare `docker run -p 8000:8000 <image>` serves the real runs with **no external
database** — which is what makes the cloud deploys below cheap and simple. Override
`DATABASE_URL` (compose, RDS, Neon, …) to use Postgres instead.

## Compose stack

[`docker-compose.yml`](../docker-compose.yml) wires the app to Postgres:

```bash
docker compose up --build      # → http://localhost:8000
```

The `app` service waits for Postgres to be healthy, **applies migrations, then serves**:

```yaml
command: sh -c "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"
```

Migrations live in the orchestrator command (not the image `CMD`) because they need a reachable
database; the image itself is a generic "serve" unit. A fresh database is empty, so the
dashboard renders its empty state until an evaluation populates it:

```bash
# run an evaluation against the same database the app uses
docker compose exec app python -m app.eval.cli --label first-run    # mock provider
# or, for a real model, set LLM_PROVIDER=groq and GROQ_API_KEY in the app environment
```

## Deploying elsewhere

The image is platform-agnostic (Fly.io, Render, Railway, a plain VM with Docker). Two things to
provide:

- **`DATABASE_URL`** — an async SQLAlchemy URL (`postgresql+asyncpg://…`). Run
  `alembic upgrade head` once against it before first serve.
- **`LLM_PROVIDER` / `GROQ_API_KEY`** — only needed to *run* evaluations; serving the dashboard
  over existing runs needs neither (`LLM_PROVIDER=mock` is the default).

## Split deploy: Vercel (dashboard) + Railway (API + Postgres)

The dashboard is static and fits Vercel; the API is a persistent process with a database and
fits a container host. The repo supports this split with no code edits at deploy time:

- the frontend reads `VITE_API_BASE_URL` (build-time) and falls back to same-origin `/api`;
- the API enables CORS from `CORS_ORIGINS` (default `*`);
- `config.normalize_database_url` coerces Railway's `postgresql://` URL to `+asyncpg`;
- the image honors Railway's injected `$PORT`; [`railway.json`](../railway.json) migrates then serves.

**1. Railway — API + database.** Create a project, add a **PostgreSQL** plugin (it exposes
`DATABASE_URL`), and deploy this repo. Railway reads [`railway.json`](../railway.json), builds the
[`Dockerfile`](../Dockerfile), runs `alembic upgrade head`, and serves on `$PORT`. Set
`CORS_ORIGINS` to the Vercel URL (or leave `*`). Note the public API URL, e.g.
`https://acs-api.up.railway.app`.

**2. Seed data.** A deployed instance can't *produce* runs — the agent's sandbox needs
`unshare --net` privileges managed hosts don't grant — so import the committed results into the
Railway database (run from a machine that can reach it, with `DATABASE_URL` set to the Railway
Postgres URL):

```bash
cd backend
DATABASE_URL="<railway-postgres-url>" uv run python -m app.eval.import_results \
  --results ../docs/results/groq-llama-3.3-70b-v1.json
DATABASE_URL="<railway-postgres-url>" uv run python -m app.eval.import_results \
  --results ../docs/results/groq-llama-3.3-70b-v2.json
```

That gives the dashboard the real v1 (86.7%) and v2 (100%) runs and a working compare view.
Imported runs have empty traces (the results JSON doesn't carry per-step traces); a full run
with traces would need to be copied from a database produced by an actual eval run.

**3. Vercel — dashboard.** Import the repo, set **Root Directory** to `frontend` (Vercel
auto-detects Vite via [`vercel.json`](../../frontend/vercel.json)), and add an environment
variable `VITE_API_BASE_URL` = the Railway API URL from step 1. Deploy. The SPA now calls the
Railway API cross-origin, which CORS permits.

## AWS

Because the image is self-contained (baked SQLite), AWS needs **no database** — just run the
container. The image works unchanged; point Postgres (RDS) at it later by setting `DATABASE_URL`.

### Option A — App Runner (managed, HTTPS, ~$5/mo)

Push the image to ECR, then have App Runner run it. [`scripts/push-to-ecr.sh`](../scripts/push-to-ecr.sh)
wraps the build/login/push:

```bash
scripts/push-to-ecr.sh us-east-1            # builds, creates the ECR repo, pushes, prints the URI
```

Then in the App Runner console: **Create service → Container registry → ECR**, pick the pushed
image, set **port 8000** and **health check path `/health`**, create. App Runner returns an HTTPS
URL. No environment variables are required — the image serves its baked data.

### Option B — EC2 free tier (free for 12 months, manual, HTTP only)

```bash
# t3.micro (Amazon Linux 2023), security group allowing :80 and :22
sudo dnf install -y docker && sudo systemctl enable --now docker
# after pushing to ECR (Option A) authenticate the box to ECR, or build on it, then:
sudo docker run -d -p 80:8000 --restart unless-stopped <ecr-image-uri>
# dashboard at http://<ec2-public-ip>   (HTTPS needs a domain + reverse proxy / ACM)
```

### Cost honesty

App Runner keeps a warm instance (~$5/mo even when idle). EC2 `t3.micro` is free for 12 months,
then bills — stop/terminate it when you're done, and watch data-transfer/EBS charges. For a
no-card, genuinely $0 option, the same image runs on Render or Koyeb free tiers (see *Deploying
elsewhere*).

## Honest notes

- **Not built in this environment.** This cloud build sandbox has no usable Docker daemon, so the
  image was not assembled here. Each stage is verified independently instead: the frontend build
  produces `dist/`; the production dependency set (`uv sync --no-dev`) imports and serves
  `app.main` with dev-only packages absent; the exact build-time seed commands populate a SQLite
  database that the API then serves (both runs + the compare); and `docker compose config`
  validates the stack. The assembly itself (`docker build`) is standard but unverified here —
  worth a real build before relying on it.
- **The production image is lean.** matplotlib (M7 figure generation) and the test stack are
  dev-only, so `uv sync --no-dev` excludes them; `aiosqlite` is a runtime dependency so the baked
  SQLite database works.
- **Single instance assumed.** Running migrations in the start command is fine for one app
  instance; a multi-replica deploy should run migrations as a separate one-shot step.
