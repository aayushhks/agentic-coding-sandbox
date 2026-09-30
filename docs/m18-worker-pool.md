# M18 — Many Workers: Heartbeats, Fencing, Retries and an Invariant Checker

Any number of workers can now share the queue on short leases. A worker whose lease has run out can
never write to its job again, even before anyone takes the job back. Infrastructure failures are
retried a bounded number of times and then dead-lettered; an agent's own failure never is. And every
run can be checked against four invariants read back from the database. The design and its
trade-offs are in [design.md](design.md).

## What was built

- **Fencing** (`fleet/store.py`). Marking a job running, a heartbeat, giving a job back and
  publishing are each accepted only from the job's current attempt while its lease is live, checked
  under the row lock against Postgres's clock. The attempt number is the fencing token. A publish
  reads the clock once and uses that reading both for the lease check and as the result's
  `published_at`.
- **Heartbeats** (`fleet/worker.py`). The worker extends its lease every third of the lease while the
  job runs. If a heartbeat finds the lease gone, the worker stops the run instead of finishing it.
  With heartbeats, the default lease drops from 10 minutes to **30 s**.
- **Bounded retries and dead letters.** A runner that raises is an infrastructure failure, and so is
  a lapsed lease. The attempt ends (`released` or `lease_expired`) and the job goes back to the queue
  after a doubling backoff (2 s, 4 s, …, capped at 60 s). Once the batch's budget (`max_attempts`,
  default 3) is spent, the job is **dead-lettered**, with no result and its last error kept. What a
  runner returns — succeeded, failed, escalated — is final on the first try.
- **Migration 0002**: the `dead_lettered` state, `max_attempts`, `available_at` and `last_error` on
  jobs, the `released` ending and its error on attempts, and two new constraints (no attempt past
  the budget; an ended attempt says how).
- **The invariant checker** (`fleet/invariants.py`), described below.
- **A worker pool.** The bench's fleet executor starts N worker processes, and
  `bench.cli replay --executor fleet --workers N` runs trials on it. Measuring how that scales is M21.

## The invariant checker

It reads a drained run back from the database — jobs, attempts and results, in one consistent
snapshot — and reports every violation of the kickoff's four invariants:

| Invariant | Checked as |
|---|---|
| Exactly one result per job | a finished job has exactly one result, the one it points at, with the same outcome |
| Nothing lost | every submitted job is present and final |
| No stale writes | each result came from the job's last attempt and worker, before that attempt's lease ran out; no two attempts of a job overlapped |
| Accounting adds up | attempt logs run 1…n and every attempt ended the way its lease allows; dead letters used their whole budget; final states sum to the jobs submitted |

**It can fail.** Its tests feed it sixteen corrupted snapshots, each caught under the invariant it
breaks (a second result, a result published a second after its lease ran out, an attempt claimed
while the previous one still held the job, a gap in the attempt log, …), and a corruption written
into a real database. What it proves and what it doesn't is in
[design.md](design.md#the-invariant-checker-what-it-proves-and-what-it-doesnt).

It now backs the M17 kill-and-restart tests too, in place of their hand-written checks.

## Fencing, with real processes

`tests/test_fleet_fencing.py` pauses a worker process with `SIGSTOP` in the middle of a job, on a
1 s lease. While it is stopped, the lease runs out, and a second worker takes the job back and
publishes it. Then the first worker resumes with `SIGCONT`:

- **When the job is longer than a heartbeat interval**, the resumed worker's next heartbeat finds its
  lease gone, and it stops the run.
- **When the job is shorter** (heartbeat set to 0.9 s, job 0.3 s), the job finishes first and its
  late result is refused.

Either way the resumed worker reports nothing published, exactly one result survives (the second
worker's, on attempt 2), and the checker passes. An in-process test covers the case where nobody has
taken the job back yet: a runner that blocks the event loop past its lease starves the heartbeats,
and its result is refused on the lapsed lease alone.

## The stress test

`tests/test_fleet_stress.py` runs as its own CI step on every push, against a `postgres:16` service,
and prints its counts. A batch of **300 jobs** runs on **8 worker processes** with **1 s leases**
(heartbeats every 0.33 s, reaping every 0.5 s, backoff from 0.05 s, 3 attempts per job). The job mix
is seeded:

| Kind | What it does | Seed 0 | Seed 1 |
|---|---|---|---|
| short | sleeps 20–300 ms and succeeds | 244 | 247 |
| long | sleeps 1.2–2 s, longer than a lease, so it only finishes if heartbeats work | 22 | 17 |
| gives up | the agent's own failure: publishes `failed`, which is final | 14 | 15 |
| always crashes | raises every time: retried, then dead-lettered | 14 | 11 |
| crashes once | raises on its first run, then succeeds | 6 | 10 |

Every 0.3–0.8 s, a worker that is in the middle of a job is either killed with `SIGKILL` and replaced
by a fresh process, or paused with `SIGSTOP` for 1.5–2.5 s — well past its lease — and then resumed.
This runs until the batch is done. Then the checker runs, along with per-kind checks:

- every crashing job is dead-lettered after 3 attempts;
- every published outcome is the one its kind allows;
- every once-crashing job that succeeded needed a second attempt;
- a job longer than a lease finished on its first attempt;
- the chaos actually happened, and fencing actually turned a resumed worker away.

### Results

Five runs of each seed, each on a fresh batch, on the machine described in the next section:

| | Seed 0 | Seed 1 |
|---|---|---|
| Runs | 5 | 5 |
| Wall clock per run | 18.6 [18.4–19.5] s | 19.4 [19.3–20.1] s |
| Workers killed / paused | 80 / 52 | 68 / 86 |
| Jobs that ran more than once | 192 of 1,500 | 210 of 1,500 |
| Attempts ended by a lapsed lease / given back | 132 / 239 | 152 / 213 |
| Late results refused / runs stopped on a lost lease | 27 / 25 | 49 / 36 |
| A lapsed job claimed again, after its lease ran out | 0.17 [0.14–0.19] s median per run, max 0.77 s | 0.17 [0.15–0.20] s median per run, max 0.71 s |
| Dead letters: always-crashing / healthy | 70 / 13 (3, 2, 3, 2, 3 per run) | 55 / 15 (4, 4, 1, 4, 2 per run) |
| Invariant violations | **0** | **0** |

**Every run passed.** The checker found no violation in any of them, and every per-kind check held.

- **Fencing did its job under load.** Across the ten runs, 76 late results were refused and 61 runs
  were stopped because their lease was gone, against 138 pauses and 148 kills. No stale result
  landed in any run.
- **Recovery is set by the lease.** A job whose worker had died was claimed again a median of
  0.14–0.20 s after its lease ran out (per run; at most 0.77 s), with reaping every 0.5 s and backoff
  from 0.05 s.
- **Heartbeats carried long jobs past their lease.** In every run, long jobs of 1.2–2 s on a 1 s lease
  finished on their first attempt, which only works if heartbeats keep extending the lease. The test
  requires it of at least one.

**A negative result: healthy jobs were dead-lettered.** Beyond the jobs that always crash, 1–4 jobs
per run that did nothing wrong were dead-lettered (28 in ten runs), because the chaos killed or
paused their worker three times. In the two runs inspected job by job, 8 of the 9 were long jobs and
one was a give-up job. Long jobs are mid-job most often, so chaos that only hits workers mid-job hits
them the most, as real machine failures would.

The retry budget counts lapsed leases because that is what stops a job that kills its worker from
looping forever. The price is that a long job on an unreliable fleet can use up its budget through no
fault of its own. The chaos here is far harsher than a real fleet's, at 25–32 kills and pauses in a
19-second run across 8 workers, but the trade-off is real. Separate budgets for lapsed leases and
runner errors, or a larger budget for lapsed leases, would soften it. M18 keeps the single budget it
was specified with.

## Did M18 cost anything? One worker, measured twice

Fencing adds work to every publish and wraps every run in a heartbeat loop, so the one-worker
comparison from M17 was repeated, twice. The setup matches M17's: the 18-task set, replay at zero
latency, seed 1, the same machine (a virtualized 4-vCPU Intel Xeon @ 2.80 GHz, 15.72 GiB RAM,
Ubuntu 24.04.4, Python 3.13.12, PostgreSQL 16.13), with Postgres, the API and the worker all on it.

**First, M17's A/B again on the M18 build**: 5 trials per arm, interleaved, with the arm order
alternating ([records](results/bench/m18/)). The fleet came out **5% slower**: 11.62 s median
[10.91–12.27] against 11.06 s [10.93–11.59]. But the per-trial difference ranged from −0.26 s to
+1.34 s, and this session ran 10% faster than M17's across the board. The same code's sequential
replay measured 12.28 s median in M17's session and 10.80 s in a later one. Numbers from different
sessions can't be compared, so this didn't settle whether M18 costs anything.

**Then, build against build, in one session.** The M17 code and the M18 code ran alternately, in ABBA
order: ten `bench.cli ab` runs of two trials each, with the arm order alternating inside each run.
Both builds ran on the same interpreter and packages, so only the repo's code differed
([records](results/bench/m18/builds/), 10 trial pairs per build).

| | M17 code | M18 code |
|---|---|---|
| Fleet minus sequential, per trial pair | **+0.18 s** median [−0.51 to +0.85] | **+0.13 s** median [−0.70 to +0.79] |
| Worker utilization | 0.991 [0.986–0.994] | 0.990 [0.986–0.994] |
| Idle gap from a publish to the next claim | 2.06 ms median (p95 3.55) | 2.84 ms median (p95 4.68) |
| First claim after submit | 54 ms median | 55 ms median |
| Job service time, fleet / sequential | 634 / 633 ms | 635 / 639 ms |

**M18 added no measurable cost at the batch level.** The fleet's overhead over sequential was
+0.18 s with M17's code and +0.13 s with M18's, both well inside the trial-to-trial spread. The 5%
from the first A/B did not reproduce. Per job, the idle gap between a publish and the next claim grew
by 0.8 ms. The likely reason is where M18 stamps a job's finish: at the lease check, one statement
earlier in the same publish transaction, so one database round trip moved out of service time and
into the gap.

One discarded attempt: the first build-against-build run gave the M17 code a fresh checkout with a
new virtualenv, and every task took twice as long, in both arms. The sandbox runs Python with
`PYTHONDONTWRITEBYTECODE=1`, so a virtualenv without cached bytecode recompiles pytest on every
sandboxed test run. Those runs measured the environment, not the code, and were thrown away.

`python3 docs/results/bench/m18/overhead.py` recomputes every number in this section, and M17's, from
the records.

## Honest notes

- **One machine.** Postgres, the workers and the test share 4 vCPUs. A 1 s lease with heartbeats
  every 0.33 s leaves 0.67 s of scheduling slack; the stress test would absorb a missed heartbeat
  as one more retry, and still has to pass the checker.
- **Chaos is kills and pauses.** Dropped database connections, API restarts and kills at each exact
  point in the protocol are the M20 fault matrix. A worker that loses its database connection today
  crashes, and its job comes back when the lease lapses.
- **The seeds fix the job mix and the chaos's choices, not the timing**, so the counts above differ
  from run to run; the checker's verdict doesn't.
- **A refused run still ran.** Fencing keeps a stale result out of the database; it does not undo what
  the job did elsewhere. That is for the controlled-execution milestone.
- **Dead letters stay dead.** There is no API to requeue one yet.
- **The replay runner never raises for a task's failure**, including a replayed provider error:
  replay reproduces it exactly, so a retry could only fail the same way. None of the committed
  recordings contain one.

## Reproduce

```bash
cd backend
uv run pytest tests/test_fleet_stress.py tests/test_fleet_fencing.py -s     # stress and pauses
uv run pytest tests/test_fleet_invariants.py                                # the checker
uv run python -m bench.cli ab --trials 5 --out-root /tmp/m18-ab             # the one-worker a/b
uv run python -m bench.cli replay --executor fleet --workers 4 --trials 1  # a worker pool
python3 ../docs/results/bench/m18/overhead.py                               # the tables above
```

The tests and the bench commands start a throwaway local Postgres when no database is given
(`TEST_DATABASE_URL` / `--database-url` to use your own).
