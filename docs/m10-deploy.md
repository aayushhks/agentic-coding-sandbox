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
   (`uv sync --frozen --no-dev` — no pytest/ruff/mypy/aiosqlite), copies the backend source, and
   copies the built SPA from the first stage into `/app/frontend/dist`.

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

## Honest notes

- **Not built in this environment.** This cloud build sandbox has no usable Docker daemon, so the
  image was not assembled here. Each stage is verified independently instead: the frontend build
  produces `dist/`, the production dependency set (`uv sync --no-dev`) imports and serves
  `app.main` with the dev-only packages absent, FastAPI serves the built SPA alongside the API,
  and `docker compose config` validates the stack. The assembly itself (`docker build`) is
  standard and unverified here — worth a real build before relying on it.
- **The runtime carries matplotlib.** It is a top-level dependency (used by the M7 figure
  generation) and so lands in the production image even though the serving path never imports it.
  A leaner image would move it to an analysis-only dependency group; left as-is to avoid lockfile
  churn in the final milestone.
- **Single instance assumed.** Running migrations in the start command is fine for one app
  instance; a multi-replica deploy should run migrations as a separate one-shot step.
