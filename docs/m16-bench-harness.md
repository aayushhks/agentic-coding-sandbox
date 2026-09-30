# M16 — Benchmark Harness and Baseline

The "before" side of every comparison in the distributed-execution work. Before any job store,
scheduler or worker pool exists, this measures **today's implementation** — one process running
tasks back to back — with the harness, task set and record format every later milestone reuses.

## Headline

All runs: the 18-task set once each (18 jobs), seed 1, **one in-process sequential worker on a
single machine** — a virtualized 4-vCPU Intel Xeon @ 2.80 GHz with no cgroup CPU quota, 15.72 GiB
RAM, Ubuntu 24.04.4, kernel 6.18.44, Python 3.13.12, sandbox network isolation active. Replay rows
are the **median [min–max] of 5 trials**; the real-model row is **one trial (N=1)**.

| Run | Batch wall clock | Tasks / min | Queue wait p50 / p95 | Service time p50 / p95 / max |
|---|---|---|---|---|
| replay, zero latency | **13.44 s** [12.77–13.61] | 80.37 [79.35–84.56] | 6.94 / 11.99 s | 0.78 / 1.13 / 1.13 s |
| replay, zero latency (repeat run) | **13.14 s** [12.89–14.03] | 82.22 [76.96–83.76] | 6.84 / 11.68 s | 0.77 / 1.12 / 1.18 s |
| replay, recorded model latency | **91.23 s** [90.60–91.33] | 11.84 [11.83–11.92] | 50.36 / 82.23 s | 4.70 / 9.86 / 10.76 s |
| real model, qwen3.8-27b (N=1) | **973.0 s** | 1.11 | 586.0 / 892.2 s | 45.2 / 123.4 / 180.1 s |

Every run, every trial: **15 solved, 2 escalated, 1 expected failure — 18/18 matched their
expected outcome**, 92 model calls, 108,804 tokens (91,083 prompt / 17,721 completion), worker
utilization 1.000, **0 replay divergences**, and identical outcomes across trials.

Records: [`results/bench/`](results/bench/) — one JSON per trial plus a `summary.json` per run.

## What the baseline says

- **The real workload is dominated by waiting.** In the real run, 881.7 s of 973.0 s (91%) went to
  Groq's free-tier rate limit (8,000 tokens per minute for this model on this key). Excluding those
  waits, a task took 4.60 s at p50 and 9.78 s at p95 — which the recorded-latency replay reproduces
  (4.70 / 9.86 s). With one worker the rate limit already binds, so extra workers cannot raise
  real-model throughput on this key; the scaling milestone measures on replay and says so.
- **Even without throttling it's I/O-bound.** In the recorded-latency replay, the model's measured
  latency accounts for 77.5 s of the 91.23 s batch (85%; 92 calls, 0.84 s mean). The rest is the
  harness itself, which the zero-latency replay isolates: 13.14–13.44 s, mostly pytest subprocesses
  (the agent's own test runs plus grading). Waiting on the model is what parallel workers can
  overlap.
- **Queue wait is the number a worker pool has to cut.** In a sequential batch a job waits for
  everything submitted before it, so p95 queue wait is 89% of the whole batch in the zero-latency run
  (11.99 of 13.44 s) and 90% in the recorded run (82.23 of 91.23 s).
- **Utilization is 1.000 by construction.** One worker running jobs back to back never idles; it is
  the reference that multi-worker runs are compared against, not a result.

## Determinism, and how noisy the clock is

- **Replay is deterministic.** Across 15 replay trials (three runs of five), every job's outcome
  was identical, there were zero divergences, and every trial matched the outcomes of the real run
  it replays. Counts, tokens and model calls are identical in every trial — those are the metrics to
  compare on when timings overlap.
- **Wall clock is not.** Two committed runs of the identical zero-latency configuration, minutes
  apart, have medians of 13.44 s and 13.14 s (2.3% apart, overlapping ranges). A first run of the
  same configuration measured 12.38 s [11.80–12.50] — 6% and 8% below them, with a range that
  overlaps neither. Its records are kept under
  [`results/bench/superseded/`](results/bench/superseded/) (see *Bugs found* below). So on this
  virtual machine, **between-session noise reaches ~8% of the median, larger than the within-run
  spread.** Consequences for later milestones: A/B comparisons will interleave the two builds in
  one session, and the deterministic metrics come first.

## The real-model trial

`qwen/qwen3.8-27b` via Groq (temperature 0, 1,024 max tokens per call — the M7 agent settings),
2026-09-30. Per-task records are in
[`sequential-real-qwen3.8-27b/trial-1.json`](results/bench/sequential-real-qwen3.8-27b/trial-1.json).

- **All 15 benchmark tasks solved**, in 4–9 model calls and 2,965–17,375 tokens each (`fizzbuzz`
  took the most: 9 calls, 17,375 tokens).
- **Both escalation tickets escalated**, with sensible reasons: TCK-04 ("make it faster") after
  listing an empty workspace; TCK-08 (cut production over to Postgres) on the first call, citing no
  code and no access to production systems.
- **The unsatisfiable task failed as designed** (`wrong_solution`): the agent implemented the
  described behavior, passed its own test, and the contradictory hidden suite failed it.
- **Cost:** $0 incurred — the key is on Groq's free tier with no payment method. A list-price cost
  isn't computed because the price couldn't be verified from this environment (groq.com is blocked
  here); records carry `cost_usd: null` rather than an estimate.
- **This is one trial (N=1).** More real trials haven't been run yet; each one is
  `bench.cli record --trial N` and regenerates the run's `summary.json`. Until then, read these
  real-model numbers as a single run.

**The M6/M7 numbers are not comparable.** 86.7% → 100% were single runs on
`llama-3.3-70b-versatile`, which Groq no longer serves (it now returns `model_not_found`), so they
can't be re-run. This baseline uses a different model and a different task set.

## How the harness works

**Task set v1** (`backend/bench/taskset.py`; its content hash is in every record):

| Tasks | Expected | Why they're in the set |
|---|---|---|
| the 15 benchmark tasks (5 easy · 5 medium · 5 hard) | solve | the M4 benchmark, with the M7 agent settings |
| TCK-04 (underspecified), TCK-08 (needs a human) | escalate | exercise the escalation path |
| `unsatisfiable_spec` | fail | its hidden tests contradict each other, so nothing can pass (a test proves the reference fails); guarantees the failure path runs |

The injection ticket (TCK-10) waits for the container sandbox (M19): recording runs the model's
shell commands live, as root, in today's process-level sandbox, and an adversarial prompt
shouldn't run there.

**Today's path, unchanged.** `SequentialExecutor` is a loop over the existing `run_task` /
`resolve_ticket`. The only change in `app/` is a backward-compatible `max_retries` argument on the
Groq provider, so the recorder — not the SDK — owns retries and can time them separately.

**Replay mode — deterministic and free.** A real run is recorded once (every response, its
tokens, the model's latency, and any rate-limit wait kept separately; one file per task in
`backend/bench/recordings/v1/`). Replay serves the responses in order and **refuses anything the
recording didn't see**: a changed system prompt or task text, different sampling settings, an
observation whose status line differs (e.g. a test that passed when recorded now fails), a request
past the end of the recording, or a run that stops early. Any of these marks the job
`replay_divergence` — a *harness* failure, never an agent failure. A provider failure that ended a
recorded run replays at the same point and stays an *infrastructure* failure. Two latency profiles:
`zero` answers instantly (stresses the harness; used in CI) and `recorded` waits each call's
measured model latency, with the rate-limit waits left out.

**Guards in CI.** A pytest check fails if any recording stops matching the agent's current opening
prompt (a prompt or tool change without a re-recording), and every push replays all 18 recorded
tasks twice and fails on any divergence or differing outcome.

**What each record holds.** Config and environment are captured by code, never typed: mode,
latency profile, executor, workers, topology, jobs, seed, task-set and recordings digests, model,
agent and sandbox settings; git commit and whether anything but the bench's own output records
differed from it; CPU model, cores, RAM, cgroup limits, virtualization, kernel, Python, and whether
the sandbox had network isolation.

**Metric definitions.**
- *Batch wall clock*: submit to last finish. *Tasks per minute*: jobs ÷ wall clock.
- *Queue wait*: submitted → claimed. *Service time*: claimed → finished (setup, agent, grading);
  also reported without retry waits.
- Percentiles use linear interpolation between closest ranks (numpy's default) and record their
  n; with 18 jobs, p99 is effectively the max.
- *Utilization*: exact busy time from each worker's claim/finish intervals ÷ (workers × wall
  clock), plus a 1-second timeline. Chosen over a sampling thread, which adds overhead and can miss
  short gaps.
- *Counts*: solved, escalated, failed — split into task (the agent), infrastructure (model API or
  sandbox) and harness (replay divergence) — retries, and matched expectations.
- *Summaries*: the median and min/max of every scalar across a run's trials, and whether all
  trials produced identical outcomes.

## Reproduce

```bash
cd backend
uv run python -m bench.cli replay --trials 5                      # zero-latency baseline
uv run python -m bench.cli replay --trials 5 --latency recorded   # recorded-latency baseline
uv run python -m bench.cli record --trial 2                       # a real trial (GROQ_API_KEY)
uv run python -m bench.cli summarize --label sequential-real-qwen3.8-27b
```

## Bugs found while building it

- **The dirty flag counted the harness's own output.** Each trial captures whether the checkout
  differs from its commit, and the records of earlier trials in the same run were untracked files,
  so 9 of the first 10 replay trials were flagged dirty with no code changed. Fixed in `578f038`
  (the bench's output directory is excluded; recordings, which are inputs, are not). The affected
  records are kept as [superseded](results/bench/superseded/) rather than deleted, and both replay
  baselines were re-run on a clean checkout.

## Honest notes

- **One machine, one worker.** Nothing here is distributed yet; that's the point of a baseline.
- **Small set.** 18 tasks characterize the execution path, not the model; outcome counts are
  descriptive, and p99 with n = 18 is close to the max.
- **Replay reproduces latency, not throttling or network jitter.** The recorded profile sleeps the
  model's measured latency per call; rate-limit waits and failures that weren't recorded can't
  appear.
- **Real-model numbers are N=1 until trials 2 and 3 are in.**
