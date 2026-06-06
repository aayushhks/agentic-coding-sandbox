# Agentic Coding Sandbox + Eval Harness

> **Live demo → https://d3co9fcex8s4iu.cloudfront.net** — the interactive dashboard, served over HTTPS from AWS (CloudFront → EC2 / Docker).

An autonomous coding agent that, given a programming task, writes code, runs it in an
isolated sandbox, observes the result, and iterates until the task's tests pass — paired
with an eval harness that measures how well the agent performs across a benchmark of tasks.

This is a portfolio project built to demonstrate the engineering behind agentic systems:
the control loop, the tool interface, the sandboxing, the failure modes, and how to measure
whether the agent is actually any good. It is the companion to `llm-eval-with-probes` — that
project evaluates LLMs; this one builds and evaluates an autonomous agent.

**Result:** on the 15-task `v1` benchmark the agent (Llama 3.3 70B via Groq) goes from
**86.7% → 100%** after two targeted hardening fixes, with the full story — runs, traces,
figures, and a regression gate — written up under [`docs/`](docs/).

## Architecture

```mermaid
flowchart LR
  subgraph Agent["agent runtime"]
    LLM["LLM provider<br/>(Groq · mock)"] --> Loop["ReAct loop"]
    Loop -- "tool calls" --> Sandbox["subprocess sandbox<br/>(network-isolated)"]
    Sandbox -- "observations" --> Loop
  end
  Bench["v1 benchmark<br/>(15 tasks)"] --> Harness["eval harness"]
  Loop --> Harness
  Harness -- "runs · traces" --> DB[("database")]
  DB --> API["FastAPI read API"]
  API --> UI["React dashboard"]
  DB --> Cmp["regression compare"] --> Gate["CI eval gate"]
```

## Tech stack

- **Backend:** Python 3.13, FastAPI, SQLAlchemy 2 (async), Pydantic v2, Alembic, structlog, Groq SDK, `uv`
- **Sandbox:** subprocess + Linux namespaces — network-isolated via `unshare --net`, rlimit CPU/memory/file-size caps, wall-clock timeout, output cap; built behind a `Sandbox` interface so a Docker backend can drop in
- **Frontend:** React 19, Vite, Tailwind v4, TypeScript — a read-only dashboard over the eval runs
- **Database:** Postgres 16

## Running the backend

Requires [`uv`](https://docs.astral.sh/uv/). `uv` provisions Python 3.13 for you.

```bash
cd backend
uv sync
uv run uvicorn app.main:app --reload
# in another shell:
curl http://localhost:8000/health
```

## Running the dashboard

The dashboard reads persisted runs and shows solve rates, per-task agent traces, and the v1→v2
regression diff. In development, run the backend and the Vite dev server side by side:

```bash
# terminal 1 — backend, pointed at a database that has runs
cd backend
DATABASE_URL="sqlite+aiosqlite:///eval.db" uv run uvicorn app.main:app --reload

# terminal 2 — frontend dev server (proxies /api to :8000)
cd frontend
npm install
npm run dev            # http://localhost:5173
```

For a single-process serve, build the SPA and let FastAPI serve it at `/`:

```bash
cd frontend && npm run build
cd ../backend && DATABASE_URL="sqlite+aiosqlite:///eval.db" uv run uvicorn app.main:app
```

Frontend checks (also run in CI): `npm run typecheck`, `npm test`, `npm run build`. See
[docs/m8-dashboard.md](docs/m8-dashboard.md) for the API and architecture.

## Deploy (Docker)

The whole app ships as one **self-contained image**: a multi-stage `Dockerfile` builds the
dashboard, bakes the committed v1/v2 runs into a read-only SQLite database, and serves the API +
dashboard from a single FastAPI process. So a bare run has data and needs no database:

```bash
docker build -t agentic-coding-sandbox .
docker run -p 8000:8000 agentic-coding-sandbox        # → http://localhost:8000 (with data)
```

For a writable Postgres setup instead, `docker compose up --build` brings up Postgres + the app
and applies migrations (the DB starts empty — seed it with
`python -m app.eval.import_results --results docs/results/groq-llama-3.3-70b-v2.json`).

Because the image is self-contained it deploys to any container host with no database. It runs
live on **AWS** — a CloudFront distribution serving HTTPS in front of an EC2 instance running the
container (the live demo link above). The same image runs on **AWS App Runner**
(`scripts/push-to-ecr.sh` → point App Runner at the image) or free, no-card hosts like
Render/Koyeb. See [docs/m10-deploy.md](docs/m10-deploy.md) for the image layout and per-platform
walkthroughs.

## Development checks

```bash
cd backend
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

## Local Postgres

```bash
docker compose up -d postgres      # from the repo root
cd backend && uv run alembic upgrade head
```

## Configuration

Copy `backend/.env.example` to `backend/.env` and fill in values. The default
`LLM_PROVIDER=mock` runs without any API key. Set `LLM_PROVIDER=groq` and
`GROQ_API_KEY=...` to use a real model.

## Benchmark

The task benchmark lives in `backend/benchmark/v1/` as one directory per task:

```text
benchmark/v1/<task_id>/
  task.json     # metadata: id, title, description, category, difficulty, tags
  workspace/    # starting files given to the agent (absent = empty workspace)
  tests/        # hidden pytest suite that defines success (never shown to the agent)
  reference/    # known-good solution, used only to validate the task is solvable
```

`v1` ships 15 tasks across `algorithms`, `bugfix`, `refactor`, `data_structures`, and
`string_manipulation` at easy/medium/hard — including deliberately adversarial ones: a
naive-recursion Fibonacci that times out, a binary search with an infinite-loop bug, and a
multi-file package task. A parametrized test runs every reference solution against its hidden
suite, so an unsolvable or broken task fails the build.

## Agent loop

The agent (`backend/app/agent/`) runs a ReAct loop: each turn the LLM emits a single JSON tool
call with its reasoning — `{"thought": ..., "tool": ..., "arguments": {...}}` — which is parsed,
executed against the sandbox, and fed back as an observation. The loop terminates when the agent
calls `finish`, hits the iteration cap, or emits too many unparseable responses in a row. Every
step (reasoning, raw output, tool call, observation, tokens) is recorded in an `AgentRun` for the
eval harness and the trace viewer. Malformed tool calls are a first-class, recorded outcome.

## Sandbox

The agent's file writes and commands run inside a `Sandbox` (`backend/app/sandbox/`). The
default `SubprocessSandbox` enforces, per command:

- **Network isolation** via a private network namespace (`unshare --net`), so sandboxed code
  has no egress — verified by a test that asserts an outbound connection fails.
- **Resource limits** via POSIX rlimits: CPU seconds, address space (memory), file size, and
  no core dumps.
- **A wall-clock timeout** — the whole process group is killed on expiry.
- **An output-size cap** so a runaway `print` loop can't blow up the agent's context.
- **A scrubbed environment** (no host secrets leak in) and **workspace confinement** (paths
  that escape the temp workspace are rejected).

**Honest boundary:** this is process-level isolation, not a container — it does not virtualize
the filesystem or PID namespace, so it protects the host far less than Docker would. It is sized
for running the benchmark's own task code, not genuinely hostile programs. The `Sandbox`
interface lets a Docker-backed implementation drop in where a daemon is available (the preferred
option on a normal machine).

## Eval harness

`backend/app/eval/` runs the agent across the benchmark and records, per task: solved?,
iterations, tool-call breakdown, wall-clock time, token usage, the full step-by-step trace, and
— on failure — a failure mode (`timed_out`, `exhausted_iterations`, `wrong_solution`,
`malformed_tool_call`, `provider_error`, `sandbox_error`). Per-run aggregates (overall solve
rate, solve rate by category, average iterations, failure taxonomy) are computed and stored.

Results persist via SQLAlchemy 2 (async): Postgres in production (Alembic migrations in
`backend/migrations/`), SQLite for tests. Run an evaluation:

```bash
cd backend
# self-contained run on SQLite:
DATABASE_URL="sqlite+aiosqlite:///eval.db" uv run python -m app.eval.cli --label smoke --create-tables
# or against Postgres, after `uv run alembic upgrade head`:
uv run python -m app.eval.cli --label my-run
```
