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
- [x] M2 — tool interface + sandbox: tool schema + subprocess sandbox (namespace network isolation, rlimits, timeout, output cap)
- [x] M3 — agent loop: ReAct loop (JSON tool-call protocol, parsing, observation formatting, iteration + malformed caps), tested against the mock provider
- [ ] M4 — task benchmark
- [ ] M5 — eval runner + persistence
- [ ] M6 — real agent run
- [ ] M7 — analysis
- [ ] M8 — dashboard
- [ ] M9 — CI eval gate
- [ ] M10 — README, docs, deploy

## Tech stack

- **Backend:** Python 3.13, FastAPI, SQLAlchemy 2 (async), Pydantic v2, Alembic, structlog, Groq SDK, `uv`
- **Sandbox:** subprocess + Linux namespaces — network-isolated via `unshare --net`, rlimit CPU/memory/file-size caps, wall-clock timeout, output cap; built behind a `Sandbox` interface so a Docker backend can drop in
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
option on a normal machine; this cloud build environment has no usable daemon).

## Limitations

Stated plainly, and expanded as the project grows:

- The benchmark will be small (15–25 tasks) — enough to characterize behavior and failure
  modes, not a statistical claim about agent capability in general.
- The agent uses a free-tier model (Llama 3.3 70B via Groq), weaker than frontier models.
  The harness is model-agnostic, so swapping in a stronger model is a config change.
- The sandbox is process-level (subprocess + Linux namespaces), not a container — see
  [Sandbox](#sandbox) for the exact boundary. A Docker backend fits behind the same interface.
