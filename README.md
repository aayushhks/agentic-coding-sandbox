# Agentic Coding Sandbox + Eval Harness

An autonomous coding agent that, given a programming task, writes code, runs it in an
isolated sandbox, observes the result, and iterates until the task's tests pass — paired
with an eval harness that measures how well the agent performs across a benchmark of tasks.

This is a portfolio project built to demonstrate the engineering behind agentic systems:
the control loop, the tool interface, the sandboxing, the failure modes, and how to measure
whether the agent is actually any good. It is the companion to `llm-eval-with-probes` — that
project evaluates LLMs; this one builds and evaluates an autonomous agent.

## Status

Work in progress, built in milestones. **Current: M1 — scaffold.**

- [x] M1 — scaffold: FastAPI skeleton, health endpoint, LLM provider abstraction (Groq + mock), CI
- [ ] M2 — tool interface + sandbox
- [ ] M3 — agent loop
- [ ] M4 — task benchmark
- [ ] M5 — eval runner + persistence
- [ ] M6 — real agent run
- [ ] M7 — analysis
- [ ] M8 — dashboard
- [ ] M9 — CI eval gate
- [ ] M10 — README, docs, deploy

## Tech stack

- **Backend:** Python 3.13, FastAPI, SQLAlchemy 2 (async), Pydantic v2, Alembic, structlog, Groq SDK, `uv`
- **Sandbox:** Docker (preferred) / restricted subprocess (fallback) — decided in M2
- **Frontend (later):** React 19, Vite, Tailwind v4
- **Database:** Postgres 16

## Running the backend (M1)

Requires [`uv`](https://docs.astral.sh/uv/). `uv` provisions Python 3.13 for you.

```bash
cd backend
uv sync
uv run uvicorn app.main:app --reload
# in another shell:
curl http://localhost:8000/health
```

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
docker compose up -d postgres
```

The database is wired in from M5 onward.

## Configuration

Copy `backend/.env.example` to `backend/.env` and fill in values. The default
`LLM_PROVIDER=mock` runs without any API key. Set `LLM_PROVIDER=groq` and
`GROQ_API_KEY=...` to use a real model.

## Limitations

Stated plainly, and expanded as the project grows:

- The benchmark will be small (15–25 tasks) — enough to characterize behavior and failure
  modes, not a statistical claim about agent capability in general.
- The agent uses a free-tier model (Llama 3.3 70B via Groq), weaker than frontier models.
  The harness is model-agnostic, so swapping in a stronger model is a config change.
- The sandbox security boundary will be documented honestly once the sandbox lands (M2).
