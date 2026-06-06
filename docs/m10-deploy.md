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

The image is platform-agnostic (Fly.io, Render, a plain VM with Docker). Two things to
provide:

- **`DATABASE_URL`** — an async SQLAlchemy URL (`postgresql+asyncpg://…`). Run
  `alembic upgrade head` once against it before first serve.
- **`LLM_PROVIDER` / `GROQ_API_KEY`** — only needed to *run* evaluations; serving the dashboard
  over existing runs needs neither (`LLM_PROVIDER=mock` is the default).

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

### Option B — EC2 free tier (free for 12 months, HTTP only)

The easiest path needs no SSH: paste [`scripts/ec2-user-data.sh`](../scripts/ec2-user-data.sh)
into the instance's **User data** at launch. On first boot it adds a swapfile (so the build
doesn't OOM on the 1 GiB box), installs Docker, builds the self-contained image from the public
repo, and serves it on port 80.

1. **Launch instance** → Amazon Linux 2023, `t3.micro` (free-tier eligible), a key pair.
2. **Network** → security group inbound: allow **TCP 80** from anywhere (and 22 if you want SSH).
3. **Advanced details → User data** → paste `scripts/ec2-user-data.sh`.
4. Launch. After ~3–5 min the dashboard is live at **`http://<instance-public-ip>`** (build logs
   are in `/var/log/cloud-init-output.log`).

To do it by hand instead (or to redeploy after a push):

```bash
sudo dnf install -y docker git && sudo systemctl enable --now docker
git clone https://github.com/aayushhks/agentic-coding-sandbox.git && cd agentic-coding-sandbox
sudo docker build -t agentic-coding-sandbox . && sudo docker run -d -p 80:8000 --restart unless-stopped agentic-coding-sandbox
```

The live demo runs exactly this EC2 setup with a **CloudFront** distribution in front for HTTPS
(its `*.cloudfront.net` certificate, auto-renewing) and an **Elastic IP** pinning the origin so the
address never rotates; a custom domain + reverse proxy (Caddy/Nginx) or an ALB with ACM also work.

### Cost honesty

App Runner keeps a warm instance (~$5/mo even when idle). EC2 `t3.micro` is free for 12 months,
then bills — stop/terminate it when you're done, and watch data-transfer/EBS charges. For a
no-card, genuinely $0 option, the same image runs on Render or Koyeb free tiers (see *Deploying
elsewhere*).

## Honest notes

- **Built and deployed.** The image is built and run in production on AWS: the EC2 bootstrap
  (`scripts/ec2-user-data.sh`) builds it from the repo and serves it, fronted by CloudFront over
  HTTPS. The frontend build produces `dist/`; the production dependency set (`uv sync --no-dev`)
  imports and serves `app.main` with dev-only packages absent; the build-time seed commands
  populate the baked SQLite database the API serves (both runs + the compare); and
  `docker compose config` validates the stack.
- **The production image is lean.** matplotlib (M7 figure generation) and the test stack are
  dev-only, so `uv sync --no-dev` excludes them; `aiosqlite` is a runtime dependency so the baked
  SQLite database works.
- **Single instance assumed.** Running migrations in the start command is fine for one app
  instance; a multi-replica deploy should run migrations as a separate one-shot step.
