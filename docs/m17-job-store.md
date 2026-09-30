# M17 — Durable Job Store and Submission API

Every job now lives in Postgres, and any process can die at any moment without losing or
duplicating work. The design, including why Postgres and `SKIP LOCKED` and what that gives up, is in
[design.md](design.md).

## What was built

- **`backend/fleet/`** — a job queue on Postgres that knows nothing about agents:
  - a schema with its own migration history (`python -m fleet.migrate`), whose check constraints make
    "finished without a result" and "claimed without a lease" impossible states;
  - store operations, each one transaction: idempotent submit, `SKIP LOCKED` claim, start, an
    all-or-nothing publish owned by the claiming attempt, and lease reaping;
  - a worker (`python -m fleet.worker --runner module:function`) that claims, runs, publishes and
    exits cleanly on `SIGTERM` after its current job;
  - a control-plane API (`uvicorn fleet.api:app`) and client: `POST /batches` (201, or 200 for a
    repeat, 409 for a reused key with different jobs), `GET /batches/{id}`, `GET /jobs/{id}`,
    `GET /jobs/{id}/result`.
- **`bench/runner.py`** — the agent runner: a self-contained replay payload in (the task spec plus
  its recorded responses), the task's execution out. Both executors now run every task through the
  same function, so an A/B differs only in the queue.
- **`bench/fleet_executor.py`** and **`bench.cli ab`** — run a batch through the real system (an API
  process, a worker process, Postgres) and compare it with today's path.

## Restart safety (the kickoff's acceptance test)

`tests/test_fleet_restart.py`: submit 50 jobs, start a worker process, `SIGKILL` it at a seeded
point (after a seeded number of results, a seeded fraction into the next job), start a fresh worker,
run to the end. The checks read the database, not the worker's output:

- **nothing lost** — every job reached a final state;
- **nothing duplicated** — exactly one result per job, the one the job points at, published by the
  job's last attempt;
- **the attempt log adds up** — attempts numbered 1…n with no gaps, every attempt closed, all but the
  last closed as `lease_expired`, the last as `published`;
- a job the killed worker was holding was re-run by the new worker.

It runs for **10 seeds on every push**, plus **2 seeds with the real agent runner** replaying the
18-task set (`tests/test_bench_fleet_restart.py`), which also checks that every task still ends
exactly as its recording says. All pass locally and on GitHub against a `postgres:16` service; CI
sets `REQUIRE_POSTGRES=1`, so these tests fail rather than skip if the database is missing.

Where a kill lands depends on timing as well as the seed. In the run recorded for this write-up, 8
of the 10 kills caught a job in flight and 2 landed between jobs (one before anything was published);
both agent-runner kills caught a task in flight. Across 12 kills, no job was lost or duplicated.

The store tests add: five simultaneous submissions with one key create one batch of 50 jobs; three
concurrent claimers split 60 jobs with none taken twice; an attempt whose lease lapsed and was
re-claimed cannot publish over its successor.

## The cost of durability (A/B)

Two arms that differ only in the executor, interleaved in one session with the order alternating
each trial (M16 measured up to ~8% drift between sessions): today's sequential in-process path
versus the fleet with one worker. The 18-task set, replay at zero latency, seed 1, **5 trials per
arm**, median [min–max]. One machine: a virtualized 4-vCPU Intel Xeon @ 2.80 GHz, 15.72 GiB RAM,
Ubuntu 24.04.4, kernel 6.18.44, Python 3.13.12, with PostgreSQL 16.13, the API and the worker all on
it. Records: [`ab-sequential-replay-zero`](results/bench/ab-sequential-replay-zero/),
[`ab-fleet-1w-replay-zero`](results/bench/ab-fleet-1w-replay-zero/).

| | Sequential | Fleet, 1 worker |
|---|---|---|
| Batch wall clock | 12.28 s [11.84–12.59] | 12.31 s [12.12–12.77] |
| Tasks per minute | 87.93 [85.80–91.20] | 87.72 [84.55–89.12] |
| Queue wait p50 / p95 | 6.28 / 10.89 s | 6.36 / 11.06 s |
| Service time p50 / p95 / max | 0.72 / 1.06 / 1.12 s | 0.71 / 1.05 / 1.12 s |
| Worker utilization | 1.000 | 0.991 [0.988–0.993] |

What didn't change, and shows the comparison is clean: per-job outcomes were identical in every trial
of both arms (15 solved, 2 escalated, 1 expected failure), as were tokens (108,804) and model calls
(92); zero replay divergences; every trial ran on a clean checkout.

**At the batch level the difference is inside the noise** — the medians are 0.03 s apart and the
ranges overlap. The measurable cost shows up per job, from the records' Postgres timestamps (90 jobs
per arm):

- **2.4 ms per job** idle between one job's publish and the next claim (median; p95 4.0 ms, max
  5.1 ms), against 0.1 ms in-process;
- **78 ms once per batch** before the first claim (median; 44–109 ms), because the idle worker was
  between polls when the batch arrived;
- **no measurable change in service time** (median 708 ms against 715 ms).

Together that is about 0.12 s of a 12.3 s batch, matching the 0.9% utilization gap. With one worker
and jobs that take most of a second, durability costs about 1%.

## Honest notes

- **One worker.** Several workers, heartbeats and fencing come in M18; until then a lease has to
  outlast the longest job, so it defaults to 10 minutes.
- **Single machine.** Postgres, the API, the worker and the harness share 4 vCPUs.
- **Replay at zero latency** is the harness's worst case for overhead: jobs are short and CPU-bound.
  With the model's real latency, the same 2.4 ms per job is a smaller share.
- **What the restart test doesn't cover yet:** several workers racing, a worker that is paused rather
  than killed and publishes late, and a dropped database connection. Those are the M20 fault matrix.

## Reproduce

```bash
cd backend
uv run pytest tests/test_fleet_restart.py tests/test_bench_fleet_restart.py -s   # kills and restarts
uv run python -m bench.cli ab --trials 5 --latency zero                          # the A/B
```

Both start a throwaway local Postgres when no database is given (`TEST_DATABASE_URL` /
`--database-url` to use your own).
