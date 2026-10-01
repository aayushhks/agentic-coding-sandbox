# M20 — Fault Injection: Seeded Faults at Named Points in the Job Protocol, and the Checks That Follow

**740 faults injected across 260 runs — 26 scenarios, 10 seeds each — and zero invariant
violations.** The faults were kills, pauses and hangs, dropped database connections, an api killed
mid-request and Postgres restarted the way a crash would. Every kill, pause, hang and drop landed at
a named point in the job protocol; the Postgres restarts came when set shares of each batch had
finished. Every run was checked by the invariant checker and by the effects its fault had to leave
behind, and every scenario also runs in CI, on every push. Along the way the faults found two real
bugs, and breaking the code on purpose found a scenario that had been passing for the wrong reason.
All three are below.

## What was built

- **Failpoints** (`fleet/failpoints.py`): fourteen named points in the job protocol that do nothing
  unless a run arms them — before a claim, a heartbeat and a publish; between the write and the
  commit of a claim, a publish, a give-back, a reap and a cancelled job's end; and just after the
  commit of a batch submit (in the api), a claim, a mark-running, a publish, a give-back and a
  cancelled job's end. An armed point fires once, on its k-th hit, and does one of three things:
  **kill** (the process SIGKILLs itself), **stop** (it SIGSTOPs itself until the harness resumes
  it, or for good) or **drop**. Inside a transaction, a drop has Postgres terminate the connection,
  so the transaction never commits; anywhere else, the call raises the error a dropped connection
  would, as if the answer to a committed write were lost on the way back. A point logs the fault
  before acting, so a run counts the faults that happened, not the ones it planned. The matrix arms
  twelve of the fourteen; the other two — just after a give-back's and a cancelled job's end commit
  — are armed only by the unit tests that drop the connection there.
- **Riding through a dropped connection** (`fleet/connections.py`). A worker whose database
  connection drops retries the call on a fresh one, for up to a minute, instead of crashing. Marking
  a job running, giving it back and ending a cancelled one each recognise their own committed first
  try, and so does a publish — but only of the very result it stored — so a call made twice counts
  once.
- **The chaos harness** (`backend/chaos/`): each scenario is a point, a fault and the effects that
  fault must leave behind. The harness plays the supervisor: it replaces dead workers, resumes
  paused ones, restarts the api, and for some scenarios restarts Postgres, claims jobs as a worker
  that never comes back, or cancels running jobs. When the batch has drained, it checks the
  invariants, every result's key and each fault's effects, and writes one record per run.
- **Exactly-once by key.** Every chaos job except the agent scenarios' recorded tasks carries a
  unique key that its result must carry back; the agent jobs must instead publish the outcome their
  recording did. The invariant checker now checks that each result carries its own job's key and
  that no key is published twice, which also catches a result landing on the wrong job.
- **CI** runs every scenario on every push, as one job per seed for three seeds, and keeps the
  records.

A run fails on any invariant violation, any result carrying the wrong key, any agent job whose
outcome is missing or differs from its recording, any fault that didn't leave its effects behind,
any planned fault that never happened, a batch that didn't drain within 90 s, or a task container
left behind — so a run where the fault missed can't pass as a clean one. A keyed job that ends
dead-lettered or cancelled, with no result, is allowed, as the invariants allow it.

## The fault matrix

Every scenario at seeds 0–9, from one clean checkout of `b64c1fd`, on one machine: a virtualized
4-vCPU Intel Xeon @ 2.10 GHz, 15.72 GiB RAM, Ubuntu 24.04.4, kernel 6.18.44, Python 3.13.12,
PostgreSQL 16.13 (a local cluster the harness owns, so it can restart it) and Docker 29.3.1
([docker.json](results/chaos/m20/docker.json)). Each run is 30 jobs (18 for the agent and
long-job scenarios), 3 workers (5 for `publish-before-stop`, below), leases of 1 s with heartbeats
every 0.33 s, reaping every 0.5 s and a budget of 5 attempts per job. A scenario arms the first
three workers it starts (only the first, for the hangs), each to fire on its first or second hit
of the point; the hang, reap and cancel scenarios fire on the first. Replacement workers start
unarmed. The api scenario instead arms each of the first three api processes to die after its
first new batch, and `postgres-restart` arms nothing: the harness restarts Postgres itself once
20%, 45% and 70% of the jobs have finished. Records: [results/chaos/m20/](results/chaos/m20/).

| Scenario | The fault | Runs | Faults | Violations | Each fault must leave behind |
|---|---|---|---|---|---|
| `claim-before-kill` | a worker is killed as it is about to claim | 10 | 30 | 0 | the process died |
| `claim-before-commit-kill` | a worker is killed with its claim written but not committed | 10 | 30 | 0 | the process died; no attempt left behind |
| `claim-before-commit-drop` | the connection drops with a claim written but not committed | 10 | 30 | 0 | the worker lived on |
| `claim-before-commit-hang` | a worker hangs for good with its claim written but not committed | 10 | 10 | 0 | another worker finished the job |
| `claim-after-commit-kill` | a worker is killed just after its claim commits | 10 | 30 | 0 | the process died; attempt ended by its lease, a later one finished |
| `claim-after-commit-drop` | a claim commits, but its answer never reaches the worker | 10 | 30 | 0 | the worker lived on; attempt ended by its lease, a later one finished |
| `start-after-commit-kill` | a worker is killed just after it marks the job running | 10 | 30 | 0 | the process died; attempt ended by its lease, a later one finished |
| `heartbeat-before-kill` | a worker is killed mid-task | 10 | 30 | 0 | the process died; attempt ended by its lease, a later one finished |
| `heartbeat-before-stop` | a worker is paused mid-task until well past its lease | 10 | 30 | 0 | attempt ended by its lease, a later one finished; woke to a lost lease, stopped the run |
| `heartbeat-before-drop` | a heartbeat mid-task fails with the error a dropped connection raises | 10 | 30 | 0 | the worker lived on; the same attempt published |
| `publish-before-commit-kill` | a worker is killed with its result written but not committed | 10 | 30 | 0 | the process died; attempt ended by its lease, a later one finished |
| `publish-before-stop` | a worker is paused well past its lease after its job ran, before it publishes | 10 | 30 | 0 | attempt ended by its lease, a later one finished; its late result refused |
| `publish-before-commit-stop` | a worker is paused well past its lease with its result written but not committed | 10 | 30 | 0 | attempt ended by its lease, a later one finished |
| `publish-before-commit-hang` | a worker hangs for good with its result written but not committed | 10 | 10 | 0 | another worker finished the job |
| `publish-before-commit-drop` | the connection drops with a result written but not committed | 10 | 30 | 0 | the worker lived on; the same attempt published |
| `publish-after-commit-kill` | a worker is killed just after its result commits | 10 | 30 | 0 | the process died; the committed result stands, no rerun |
| `publish-after-commit-drop` | a result commits, but its answer never reaches the worker | 10 | 30 | 0 | the worker lived on; the committed result stands, no rerun; counted as published, not refused |
| `release-before-commit-kill` | a worker is killed giving back a job whose runner crashed, before the commit | 10 | 30 | 0 | the process died; attempt ended by its lease, a later one finished |
| `reap-before-commit-kill` | a worker is killed reaping lapsed leases, before the commit | 10 | 30 | 0 | the process died; the lapsed lease reaped once, later |
| `cancel-before-commit-kill` | a worker is killed ending a cancelled job, before the commit | 10 | 30 | 0 | the process died; cancelled, no result |
| `api-submit-after-commit-kill` | the api is killed after a batch commits, before it answers | 10 | 30 | 0 | the process died; the batch made once, the client got it back |
| `postgres-restart` | Postgres stops the way a crash would and starts again, mid-batch | 10 | 30 | 0 | every worker rode through |
| `agent-heartbeat-before-kill` | a worker is killed mid-task, running the real agent on recorded tasks | 10 | 30 | 0 | the process died; attempt ended by its lease, a later one finished |
| `agent-publish-before-commit-kill` | a worker is killed with an agent's result written but not committed | 10 | 30 | 0 | the process died; attempt ended by its lease, a later one finished |
| `container-heartbeat-before-kill` | a worker is killed mid-task while its job runs in a container of its own | 10 | 30 | 0 | the process died; attempt ended by its lease, a later one finished |
| `container-publish-before-commit-kill` | a worker is killed with a container's result written but not committed | 10 | 30 | 0 | the process died; attempt ended by its lease, a later one finished |
| **all** | | **260** | **740** | **0** | |

**All 260 runs passed**: 740 of 740 planned faults happened — 450 kills, 150 drops, 110 pauses and
hangs, 30 Postgres restarts — and the checks found nothing. Of the drops, 60 terminated a
connection mid-transaction (before the claim's and the publish's commits) and 90 raised the error a
dropped connection raises (after those commits, and before a heartbeat). Across the runs, 493 jobs
took more than one attempt and the harness started 1,260 worker and api processes. A run took a
median 7.2 s.

What the scenarios cover, by the milestone brief's list:

| Asked for | Scenarios |
|---|---|
| Kill a worker before claim | `claim-before-kill` |
| … mid-task | `heartbeat-before-kill`, `agent-heartbeat-before-kill`, `container-heartbeat-before-kill` (and `start-after-commit-kill`, just before a run begins) |
| … after the result is written, before commit | `publish-before-commit-kill`, `agent-publish-before-commit-kill`, `container-publish-before-commit-kill` |
| … after commit | `publish-after-commit-kill` |
| Pause a worker long enough to expire its lease | `heartbeat-before-stop`, `publish-before-stop`, `publish-before-commit-stop` |
| Restart the scheduler | there is no scheduler process — workers pull, and Postgres holds all state — so: `api-submit-after-commit-kill` (the api, mid-request) and `postgres-restart` |
| Drop the Postgres connection | the five `-drop` scenarios: on both sides of the claim's and the publish's commits, and before a heartbeat |

## What the faults found

### A worker frozen mid-transaction held its job forever

`claim-before-commit-hang` and `publish-before-commit-hang` stop a worker for good in the middle of
a transaction. Before the fix, both failed on every seed: the job the frozen worker held was never
finished, and the batch never drained
([records](results/chaos/m20-before-idle-timeout/), at `5abadb6`).

The frozen transaction still held the job's row lock, and claims and reaps take rows with
`SKIP LOCKED`, so every one of them passed that row by. A frozen claim had never committed: its job
stayed queued, and every other claim skipped it. A frozen publish held a running job whose lease
ran out, and the reaper could not lock the row to end it. Either way, nothing would ever take the
job back while the worker stayed frozen.

The fix: worker sessions set Postgres's `idle_in_transaction_session_timeout` to the lease. A
session idle inside a transaction for that long is ended by Postgres and its transaction rolls
back: a frozen claim's job is simply queued again for the next claim, and a frozen publish's lapsed
lease is reaped like any other. A worker that wakes later finds a dropped connection to ride
through, and the job no longer its own. The test for it holds a job's row in an open transaction
and checks another worker can claim the job once the session has been ended.

| The two hang scenarios | Runs | Passed | Batch drained |
|---|---|---|---|
| Found, at `5abadb6`, before the fix | 6 (3 seeds each) | 0 | 0 |
| The fix undone on the final code ([below](#is-the-harness-real)) | 4 (2 seeds each) | 0 | 0 |
| With the fix, in the matrix | 20 (10 seeds each) | 20 | 20 |

This also changed what one pause has to leave behind. A worker paused well past its lease with its
result written but not committed now has its session ended by Postgres before it wakes, so its
result never lands and the job runs again: `publish-before-commit-stop` expects exactly that.

### The agent's grading starved its worker's heartbeats

The first CI runs of the chaos job failed in the agent scenarios: a healthy job ended with no
result. The invariants held, so each of those jobs had ended the one way left without a result —
dead-lettered, its five attempts spent. The agent runners graded each task by running its hidden
tests with a synchronous `pytest`, called from inside the async runner — on the worker's event
loop. For as long as `pytest` ran, the worker could not send a heartbeat. With the scenarios' 1 s
lease, a task's lease could lapse during its grading; its result was then refused and the job ran
again. The agent's own tool calls already ran in a thread; the grading didn't. Both runners now
grade in a thread too.

| CI | Builds | Agent scenario runs | Failed |
|---|---|---|---|
| Before the fix | `755cb97`, `c15556e` | 12 (2 scenarios × 3 seeds × 2 pushes) | **5** |
| After the fix | `54d90dd`, `9e54ee7`, `b64c1fd` | 18 | **0** |

The pair that differ only in the fix is `c15556e` (3 of 6 failed) and `54d90dd` (0 of 6). The CI
logs from before the fix don't show how each attempt ended, so that the lapses there used up the
retry budget is the most likely reading, not one the logs show.

The same fix as an a/b on this machine: the final code against the final code with only this fix
undone, both agent scenarios at seeds 0–9, pinned to two of the four CPUs, alternating which build
went first ([records](results/chaos/m20-agent-grading/), with the script that ran them and the
undone fix's exact diff):

| | Grading on the event loop | Grading in a thread (the fix) |
|---|---|---|
| Runs | 20 | 20 |
| Late results refused | **10**, in 7 runs | **0** |
| Leases a worker logged losing | 1 | 0 |
| Jobs that took more than one attempt | 66 | 60 (one per kill) |
| Runs failed | 0 | 0 |

Here, every lapse was retried and finished within the budget, so no run failed.

**What it matters for.** At the fleet's default 30 s lease, heartbeats come every 10 s, and the
loop would have to block for 20 s to lose a lease. The chaos runs' 1 s leases leave 0.67 s, a
margin grading used up here on two CPUs, and on CI. So this was a real bug — a worker running agent
jobs in its own process couldn't heartbeat while grading — whose effect at default settings would
need much slower grading than the bench's tasks have.

### A test that passed for the wrong reason

Before `c15556e`, `publish-before-stop` paused a worker after its job ran and woke it on a timer,
well past its lease. It passed. Then a build with fencing removed — a publish no longer checked
which attempt it was for — passed it too, in both runs
([records](results/chaos/m20-mutations-first-try/), at `66ef417` plus that change). The
scenario's three workers were all armed to pause, so while they were paused no worker was free to
take their jobs over, and a woken worker's late publish was refused by the checks that remained:
its lease had lapsed, or its job had already been reaped back to the queue. The attempt check —
fencing — was never needed.

The scenario now wakes a paused worker only once a later attempt holds its job, adds two unarmed
workers so one is free to take the job over, and fails a run where that never happens. The same
broken build then failed it in every run (below). This is what running each property's scenarios
against a build broken on purpose is for: a pass that should have been a failure looks like any
other pass.

## Is the harness real?

A checker that never fails proves nothing. Each build below is the final code (`8117d39`, whose
backend is the same as the head's) with one deliberate bug, run against the scenarios that should
catch it; the exception is the second row, `66ef417` with the same fencing change. Records:
[results/chaos/m20-mutations/](results/chaos/m20-mutations/) and
[results/chaos/m20-mutations-first-try/](results/chaos/m20-mutations-first-try/), each with the
exact diff and the script that ran it.

| Broken on purpose | The one change | Run against | Caught | By |
|---|---|---|---|---|
| Fencing | a publish no longer checks which attempt it is for | `publish-before-stop`, 5 seeds | **5 of 5 runs**, 120 violations | the invariants — a result from a superseded attempt, overlapping attempts — and the effects |
| … against the scenario before `c15556e` | the same | that version (woken on a timer, three workers all armed), 2 seeds | **0 of 2** | nothing: see above |
| The reaper | lapsed leases are never ended | `heartbeat-before-kill`, 2 seeds | **2 of 2**, 22 violations | jobs left running, the effects, and a batch that never drained |
| The idle-transaction timeout | worker sessions are never ended for idling in a transaction | the two hang scenarios, 2 seeds each | **4 of 4**, 18 violations | jobs left unfinished, the effects, and batches that never drained |
| A publish that remembers | a publish sent again after a lost answer no longer recognises its own committed result | `publish-after-commit-drop`, 5 seeds | **5 of 5**, 15 violations | the effects alone: the worker counted its own result as refused. The invariants held — the stored result was right — so they alone would have missed it |
| The claim's lock | the oldest queued job is picked without locking it, so two workers can take it at once | `claim-before-kill`, 10 seeds | **3 of 10**, 9 violations | the invariants: the same job claimed by two workers at once, the first attempt never ended |

The claim's lock is the weak spot. A double claim needs two workers to claim in the same instant,
which `claim-before-kill` produces only sometimes. In all three runs that caught it, both claimers
were replacement workers — expected, since the original workers die at their first or second claim
and replacements make nearly every claim after that. The store's own concurrency test, which has
three claimers drain one queue at once, caught the same build in 5 of 5 runs
([output](results/chaos/m20-mutations/claim-without-locking/store-test.txt)), and it runs on every
push. So that test guards the lock reliably; the fault matrix catches its loss only some of the
time.

## Did M20 cost anything?

M20 put fourteen failpoints across the worker's steps and the api's submit, wrapped every worker
store call in a retry, and added a recognition query on the failure path of four writes. The
one-worker a/b from M17 and M18 — sequential against a one-worker fleet, the 18-task set replayed
at zero latency — was run by the M19 code and the M20 code alternately, in ABBA order, ten runs of
two trial pairs each, on one interpreter and package set, in one session
([records](results/bench/m20/builds/)).

| Median [min–max] | M19 code | M20 code |
|---|---|---|
| Batch wall clock, sequential | 11.48 s [11.03–11.63] | 12.09 s [11.02–12.31] |
| Batch wall clock, one-worker fleet | 11.58 s [10.99–12.13] | 12.09 s [11.01–13.25] |
| Fleet minus sequential, per trial pair | +0.26 s [−0.22 to +0.64] | +0.15 s [−1.05 to +1.26] |
| Idle gap from a publish to the next claim | 3.12 ms (p95 4.60) | 3.15 ms (p95 5.37) |
| Worker utilization | 0.989 | 0.991 |
| Outcomes | identical across arms, every pair | identical across arms, every pair |

What the fleet adds over sequential didn't grow, and the median gap between one job's publish and
the next claim — where the claim and publish failpoints and their retry wrappers sit — didn't move,
though its tail rose (p95 4.60 to 5.37 ms). **But both arms were slower with the M20 code**: the
sequential batch by a median 0.62 s, and each graded task by a median 36.1 ms, slower in all 16.
That is not noise: 9 of the 10 M20 sequential trials were slower than every M19 trial (one-sided
exact rank test, p = 0.0008). The sequential arm runs no fleet code; the only M20 change on its path
is the grading moved into a thread. So, two follow-ups, sequential replay only, 12 trials per arm:

| | Batch wall clock, median [min–max] | Per graded task |
|---|---|---|
| Three fresh worktrees side by side, order rotating — M19 code | 11.60 s [11.06–12.75] | |
| … M20 code | 11.67 s [11.40–13.02] | −4.2 ms against M19, slower in 6 of 16 |
| … M20 with only its grading change undone | 11.79 s [11.07–12.26] | +1.6 ms against M20, slower in 9 of 16 |
| The same M20 code, from a fresh worktree | 11.98 s [11.43–13.71] | |
| … and from the main checkout, alternating | 12.14 s [11.53–13.04] | +7.2 ms against the worktree, slower in 9 of 16 |

Set up identically, the M20 code matched the M19 code, and undoing the grading change made no
difference. The first comparison ran the M20 code from the main checkout and the M19 code from a
fresh worktree, but the checkout alone made only 0.16 s — within the noise, and too little to
account for 0.62 s. **So the cost of M20 is not settled**: the matched comparison found none; the
first, which did not reproduce and which nothing measured since explains, found up to 0.62 s per
18-task batch (5%), on a path whose only M20 change costs nothing measurable on its own.

## What the checker proves, and what it doesn't

After every run, four checks, in [`chaos/harness.py`](../backend/chaos/harness.py) and
[`fleet/invariants.py`](../backend/fleet/invariants.py):

1. **The invariants**, read back from Postgres in one snapshot: exactly one result for each job
   that finished with an outcome, and none for one cancelled or dead-lettered; every result
   carrying its own job's key, and no key published twice; nothing lost; no stale write; accounting
   that adds up ([design](design.md#the-invariant-checker-what-it-proves-and-what-it-doesnt)).
2. **The outcomes**: every agent job published the outcome its recording did, with no replay
   divergence. This is what caught the dead-lettered agent jobs, which the invariants allow.
3. **Each fault's effects**, from the database and the processes' own logs: for a kill after a
   claim committed, that the attempt ended by its lease and a later attempt finished the job; for a
   pause mid-task, that the woken worker found its lease gone and stopped; for a pause before
   publishing, that its late result was refused; for a dropped connection, that the worker lived on
   and — depending on where it dropped — that the same attempt published, or that its committed
   result stood and was counted as published; and so on — the last column of the matrix.
4. **That the faults happened**: every planned fault is in the fault log — written by the process
   it hit just before it acted, or by the harness for a Postgres restart — the batch drained within
   90 s, and no task container was left behind.

**What that proves**, run by run: at each of the twelve points the matrix arms, and across Postgres
restarts, the fault left a state every check above accepts. Every job that finished with an outcome
has exactly one result, its own, written by the only attempt entitled to publish it; the jobs that
were cancelled or dead-lettered ended with none (198 of the 7,440, all by design: 100 cancelled, 98
that always crash); and each fault left the effects its scenario lists.

**What it doesn't:**

- **Points between the points.** Worker and api faults land at twelve of the fourteen named points;
  the other two are covered only by unit tests that drop the connection there, and Postgres restarts
  land wherever the workers are when a share of the jobs has finished. A kill anywhere else is
  covered only by tests that kill at moments timing decides — the M18 stress test, the M17
  kill-and-restart test and the container kill test, all on every push.
- **Faults together.** Each run injects one kind of fault at one point, up to three times.
- **Faults nobody injects.** Network partitions that leave a connection hanging rather than
  closing it, a full disk, a clock stepping, a dying Docker daemon.
- **That the checks catch everything.** The broken builds above show they catch five particular
  bugs — one of them only sometimes — not that no bug gets through. One scenario passed a broken
  build until it was fixed, and others may have blind spots no broken build has probed yet.

## Honest notes

- **One machine.** Workers, the api, Postgres and the harness share 4 vCPUs. CI runs the same
  scenarios on GitHub's runners, at three seeds per push.
- **The harness changed while M20 was built.** Three CI runs failed because a planned fault never
  happened, for two reasons: a drop meant for a heartbeat was armed on jobs too short to heartbeat,
  and — as the code and the fix suggest, since no logs from that run survive — a freshly started,
  unarmed worker did the reaping an armed one was waiting to do. Each was fixed in the harness —
  jobs got longer, workers are armed for their first or second hit, and a killed reaper is replaced
  only once its faults are spent. The platform was not at fault in any of them, and each shows the
  "every planned fault happened" check working.
- **Short steps still run on the loop.** The grading now runs in a thread, as the agent's tool
  calls do, but the runners still write a task's files and probe the sandbox's isolation on the
  event loop. Neither was timed here, and the isolation probe can block for up to 5 s per namespace
  set it tries — more than a 1 s lease's 0.67 s of slack, far less than the default lease's 20 s.
- **Seeds fix the plan, not the timing.** A seed fixes the jobs, the hits the faults fire on and
  the pause lengths; which job a fault lands on depends on timing, so two runs of one seed differ.
  The checks don't.
- **Short leases.** Every run uses 1 s leases so that lapses happen in seconds; the default is 30 s.

## Reproduce

```bash
cd backend
uv run python -m chaos run --scenarios all --seeds 0-9 --out ../docs/results/chaos/m20
uv run python -m chaos summarize ../docs/results/chaos/m20
uv run pytest tests/test_fleet_failpoints.py tests/test_fleet_connections.py tests/test_chaos.py
python3 ../docs/results/chaos/analysis.py           # every number from the chaos records
python3 ../docs/results/bench/m20/overhead.py       # the build comparisons
```

The chaos command starts a throwaway local Postgres it can restart; the container scenarios need a
Docker daemon and the task image (`scripts/build-task-image.sh`).
