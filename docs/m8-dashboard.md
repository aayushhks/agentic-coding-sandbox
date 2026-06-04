# M8 — Eval Dashboard

A web dashboard over the persisted benchmark runs. Everything before M8 produced data on
the command line — solve rates, failure taxonomies, traces, and the M7 regression diff all
lived in the database and in JSON. M8 puts a read-only UI in front of it so a run can be
explored visually: the per-category solve rates, the failure taxonomy, every task's
step-by-step agent trace, and a baseline-vs-candidate diff.

## Architecture

Two pieces behind one origin:

- **Backend read API** (`backend/app/api/`) — FastAPI endpoints over the existing
  `BenchmarkRun` / `TaskResult` tables. No new storage; it reads what the eval harness (M5)
  already writes. A `get_session` dependency yields an async SQLAlchemy session from a factory
  created in the app's lifespan.
- **Frontend SPA** (`frontend/`) — React 19 + Vite + Tailwind v4 + TypeScript. A typed client
  (`src/api.ts`) calls the API; the views render runs, traces, and diffs.

In development the two run as separate processes and Vite proxies `/api` to the backend. In
production there is a single process: `vite build` emits `frontend/dist/`, and FastAPI mounts
it at `/` (the explicit `/health` and `/api/*` routes are registered first, so they take
precedence over the static SPA mount).

```
dev:   browser → vite :5173  ──/api proxy──▶  uvicorn :8000 (FastAPI + DB)
prod:  browser → uvicorn :8000  (serves /api  and  the built dist at /)
```

## API

All endpoints are read-only and return JSON. Schemas live in `app/api/schemas.py`.

| Method & path | Returns |
|---|---|
| `GET /api/runs` | every run (summary only), newest first |
| `GET /api/runs/{id}` | one run plus a per-task summary table (no traces) |
| `GET /api/runs/{id}/tasks/{task_id}` | one task's full record, including its step trace and eval output |
| `GET /api/compare?baseline={label}&candidate={label}` | the M7 regression diff between two runs |

The list and run-detail payloads deliberately omit the heavy per-step traces; a trace is only
fetched when a specific task is opened. `/api/compare` reuses the exact `compare_runs` logic
from M7 — the dashboard's diff and the CLI's diff are the same code. Missing runs return 404;
comparing runs that cover different task sets returns 422.

## The UI

- **Runs** — a sidebar lists every run with a solve-rate pill; selecting one shows headline
  stats (solve rate, solved/total, average iterations, total tokens), a horizontal bar chart of
  solve rate by category, the failure taxonomy, and the per-task table. Clicking any task row
  slides in its **agent trace**: each step's tool call, arguments, reasoning, and observation,
  followed by the evaluation output.
- **Compare** — pick a baseline and candidate label and get the per-task transition table
  (converted / regressed / unchanged) with iteration and token deltas, mirroring the M7
  figures. This is the same diff that gated M7, now interactive.

## How to run

```bash
# backend (terminal 1) — point it at a database that has runs
cd backend
DATABASE_URL="sqlite+aiosqlite:///eval.db" uv run uvicorn app.main:app --reload

# frontend (terminal 2) — dev server with hot reload, proxies /api to :8000
cd frontend
npm install
npm run dev        # open http://localhost:5173
```

For a single-process production-style serve, build the SPA first and let FastAPI serve it:

```bash
cd frontend && npm run build          # emits frontend/dist/
cd ../backend
DATABASE_URL="sqlite+aiosqlite:///eval.db" uv run uvicorn app.main:app   # dashboard at /
```

## Testing & CI

- **Backend:** `tests/test_api_runs.py` drives the app through `httpx.ASGITransport` against a
  seeded temp-SQLite database (one event loop, no real network), covering the runs list, run
  detail, task trace, the 404s, and the compare endpoint's conversion result.
- **Frontend:** Vitest + Testing Library cover the formatting helpers and the runs list
  (render + click), and `tsc --noEmit` typechecks the whole app. `npm run build` is itself a
  gate — a type error fails the build.
- **CI:** `.github/workflows/ci.yml` gains a `frontend` job (`npm ci` → typecheck → test →
  build) running alongside the existing backend job.

## Honest notes

- **Read-only and unauthenticated.** The API only reads; there is no auth, rate limiting, or
  pagination. It is sized for a portfolio dashboard over a modest number of runs, not a
  multi-tenant service.
- **No live run-triggering.** The dashboard observes runs the eval CLI has already produced; it
  does not start agent runs itself.
- **The built `dist/` is not committed.** FastAPI serves it only when it has been built, so the
  API runs fine without a frontend build (handy for backend-only tests and deploys).
