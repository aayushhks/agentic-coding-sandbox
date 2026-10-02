# M21 — Scaling: the Task Set on 1 to 16 Workers, and What Stops It

The fixed task set, run through the fleet on pools of 1, 2 and 4 workers and, where the question
needed it, 8 and 16. For each pool size: tasks per minute, queue wait, worker utilization, where the
CPU went and how long the workers waited on Postgres. **Every process ran on one machine** — the
workers, the fleet api, Postgres and the bench driving them. This is one host's scaling curve, not a
cluster's: the workers compete with each other and with the database for the same 4 vCPUs, memory,
disk and kernel.

**The machine**, as every record captures it: a virtualized Intel Xeon @ 2.10 GHz with 4 logical
CPUs and no cgroup CPU quota, 15.72 GiB RAM, Ubuntu 24.04.4, kernel 6.18.44 (`fc-v51`), Python
3.13.12, PostgreSQL 16.13, Docker 29.3.1 ([docker.json](results/bench/m21/docker.json)). Idle, it
was 0.8% busy ([idle.json](results/bench/m21/idle.json)). Every scaling number below comes from runs
on this one VM, at commit `f74eb39` with a clean checkout. The start-up experiment and the first
runs, kept for comparison, ran on an earlier VM of the same type ([notes](#honest-notes)).

## Headline

Each batch is 72 jobs — the 18-task set four times over, in a seeded order (seed 1) — submitted at
once. Replay makes the work identical in every trial: every job of every trial in every pool came
out the same (60 solved, 8 escalated, 4 expected failures). Medians over trials, with ranges;
speedup compares each trial with the smallest pool's trial from the same round.

**Jobs at full speed** (zero latency: the model's recorded responses returned at once, so a job is
the agent loop and its sandboxed commands, all CPU), 5 trials per pool:

| Workers | Tasks / min | Speedup | Queue wait p50 / p95 | Utilization | Host CPU busy |
|---|---|---|---|---|---|
| 1 | 89.0 [87.0–89.5] | 1 | 24.41 / 45.32 s | 0.992 | 26.9% |
| 2 | 177.8 [171.3–180.0] | 2.00 [1.97–2.01] | 11.95 / 22.25 s | 0.978 | 50.9% |
| 4 | 336.6 [331.9–345.6] | 3.76 [3.73–3.97] | 6.03 / 11.60 s | 0.957 | 93.3% |
| 4, against 8 in a run of their own | 331.0 [318.8–345.0] | 1 | 6.00 / 11.70 s | 0.961 | 93.7% |
| 8 | 328.3 [323.1–333.2] | 0.98 [0.95–1.04] of 4 | 5.79 / 11.15 s | 0.948 | 96.2% |

**Jobs at the model's latency** (each response returned after the latency the real model took, so a
job mostly waits, as it does on a real model with no rate limit), 5 trials per pool:

| Workers | Tasks / min | Speedup | Queue wait p50 / p95 | Utilization | Host CPU busy |
|---|---|---|---|---|---|
| 1 | 12.0 [11.9–12.0] | 1 | 180.44 / 341.39 s | 0.999 | 5.6% |
| 2 | 23.8 [23.7–23.9] | 1.99 [1.98–1.99] | 89.53 / 169.17 s | 0.992 | 9.0% |
| 4 | 46.9 [46.8–47.1] | 3.92 [3.92–3.92] | 44.27 / 82.42 s | 0.977 | 15.5% |
| 8 | 91.8 [91.7–91.9] | 7.66 [7.64–7.69] | 19.98 / 40.32 s | 0.957 | 28.7% |
| 16 | 163.1 [162.1–163.8] | 13.64 [13.50–13.67] | 8.94 / 18.20 s | 0.857 | 49.1% |

**Each job in a container of its own**, at the model's latency, 3 trials per pool:

| Workers | Tasks / min | Speedup | Queue wait p50 / p95 | Utilization | Host CPU busy |
|---|---|---|---|---|---|
| 1 | 8.9 [8.8–8.9] | 1 | 241.99 / 458.54 s | 0.999 | 11.2% |
| 2 | 17.6 [17.6–17.7] | 2.00 [1.97–2.00] | 120.63 / 227.70 s | 0.993 | 20.7% |
| 4 | 34.5 [34.5–34.6] | 3.89 [3.87–3.92] | 60.03 / 112.16 s | 0.970 | 38.0% |
| 8 | 63.9 [63.0–64.1] | 7.19 [7.10–7.26] | 28.95 / 57.25 s | 0.953 | 68.5% |
| 16 | 78.3 [77.7–81.0] | 8.90 [8.74–9.09] | 21.78 / 43.44 s | 0.917 | 93.6% |

**The bottleneck, and the measurement that identified it.** On one host, scaling stops where the
jobs run out of the host's CPUs, and the model's latency only postpones it. With jobs at full speed,
4 workers kept the 4 vCPUs 93% busy — 98% of it the workers' own agent loops and sandboxed commands
— and 8 workers finished no more jobs than 4, because each job took twice as long while some
runnable task waited for a CPU 89% of the time (Linux's CPU pressure counter; 5.5% at 4 workers). In
containers a job costs 2.6–3.0 CPU-seconds instead of 0.7–0.8, so the same wall comes at 16 workers
and 78 jobs a minute, with the host 94% busy and a task waiting for a CPU 71% of the time. At the
model's latency the CPUs never filled (49% busy at 16 workers): what bent that curve was the batch
itself, whose longest jobs kept running after most of the 16 workers had run out of work — 13.6% of
their time — while dealing the same measured jobs to 16 workers with no time lost at all would take
26.35 s against the 26.49 s measured. Postgres was never the limit: a job's store calls took 9–15 ms
and Postgres used at most 2.6% of one CPU. With the real model, the provider's rate limit comes
before all of these: M16's key allowed about 1.3 tasks a minute at any pool size.

Queue wait here is a job's place in the batch: all 72 jobs arrive at once, so the median job waits
for about half the batch, shared among the workers. It falls as the pool grows because the batch
does; the fleet's own delays are in the idle and dealt-batch figures below.

## Where scaling stops, and why

### Jobs at full speed: the host's CPUs

Linear to 2 workers, 94% efficient at 4, and flat past the core count: in a run of their own, 8
workers finished 0.98 times the jobs 4 did. Three measurements say the 4 vCPUs are why:

- **They were full.** At 4 workers the host was 93.3% busy over the batch, and 98% of that was the
  workers and the sandboxed commands they ran: 0.656 of the 0.670 CPU-seconds each job cost. The
  api, Postgres and the bench took 3–4 ms a job each.
- **Past the cores, jobs queued for a CPU instead of running.** At 8 workers some runnable task was
  waiting for a CPU 89.1% of the time (Linux's CPU pressure counter), against 5.5% at 4. Each job's
  service time doubled, from 0.69 s to 1.39 s, while the workers' idle time barely moved
  (utilization 0.948 against 0.961): every bit of the loss is slower jobs.
- **The curve flattened where the CPU cost says it must.** The 4 vCPUs divided by the CPU a job cost
  give the rate at which every CPU would be busy: 358 jobs a minute for 4 workers in the full-speed
  run, 340 for 8 in the run past the cores. The fleet reached 94% and 97% of it.

At 4 workers the 6% lost against linear splits into slower jobs and idle workers, both measured.
Jobs took 3% longer than on one worker (0.689 s against 0.668 s), with the host 93% busy and a task
waiting for a CPU 5.4% of the time. The workers sat idle 1.2% of the batch before their first claim,
0.5% between jobs, and 2.8% after their last job while the batch's final jobs finished.

CPU per job fell as the pool grew, from 0.733 s on one worker to 0.670 s on four, while the workers'
own share stayed at 0.648–0.656 s. What shrank is the cost that accrues per second rather than per
job — the bench polling the api, the host's background work — spread over a shorter batch.

### Jobs at the model's latency: the end of the batch

At the model's latency a job spends most of its 5 s waiting, so the pool scales almost linearly to 8
workers (7.66×) and reaches 13.64× at 16. The host is not what bends it there: the CPUs were 49%
busy at 16 workers, a task waited for a CPU 4.1% of the time, and a job took 5.04 s against 5.01 s
on one worker. Utilization is what fell, to 0.857, and almost all of the idle time came after each
worker's last job: 13.6% of the workers' time at 16, against 3.9% at 8 and 2.1% at 4.

That is the batch's own shape. Its 72 jobs average 5.0 s, but the four runs each of `fizzbuzz` and
`topological_sort` take about 10 s. Sixteen workers have only 4.5 jobs each, so when the queue runs
dry the last long jobs are still running. Every 16-worker batch ended the same way: the first
workers ran out of work at 20.1–20.3 s, half had by 22.6–22.7 s, and the batch ended at 26.4–26.7 s
with a `topological_sort` job, the 65th of the 72 to be claimed, picked up at 16.7–17.0 s and
running 9.6–9.7 s.

To separate that from anything the fleet did, the analysis deals each trial's measured service
times, in the order the jobs were claimed, to whichever worker is free first, with no time lost
between jobs:

| Workers | Batch, measured | The same jobs dealt with nothing in between | Added by the fleet | Even split of the work | Longest job |
|---|---|---|---|---|---|
| 1 | 360.99 s | 360.54 s | 0.45 s | 360.54 s | 10.62 s |
| 2 | 181.57 s | 181.27 s | 0.27 s | 180.00 s | 10.65 s |
| 4 | 92.03 s | 91.86 s | 0.16 s | 89.82 s | 10.60 s |
| 8 | 47.04 s | 46.88 s | 0.16 s | 45.03 s | 10.63 s |
| 16 | 26.49 s | 26.35 s | 0.13 s | 22.69 s | 10.72 s |

The fleet added 0.13–0.45 s to a batch — first claims, and the gaps between a publish and the next
claim — at every pool size. The 3.8 s between an even split and the measured 16-worker batch is the
job mix: dealt by a dispatcher that wastes nothing, the same jobs in the same order take 26.35 s. A
continuous stream of batches would fill those idle workers with the next batch's jobs; a single
fixed batch can't.

### Each job in a container: the host's CPUs again, much sooner

Containers scale like the in-process pool to 4 workers (3.89×), lose 10% at 8 (7.19×), and get only
23% more from doubling to 16 (8.90×). It is the CPUs again, and much sooner, because a job in a
container costs three to four times the CPU of the same job in a worker's process: 2.65 CPU-seconds
against 0.80 at 4 workers. Per job at 4 workers, before the CPUs were contended:

| Where the CPU went | Per job |
|---|---|
| inside the job's container: the agent, its sandboxed commands, and a fresh Python interpreter importing the agent's code | 1.699 s |
| the Docker daemons | 0.198 s |
| no process tree: container shims, `runc`, the kernel setting up and tearing down each container | 0.625 s |
| the worker, which only drives Docker; the api, Postgres and the bench | 0.124 s |

The same job in a worker's own process cost 0.67 CPU-seconds there, because the worker imports the
agent's code once rather than per job ([M19](m19-controlled-execution.md) timed that import at 0.88
s in a container). At 16 workers the CPUs were 93.6% busy, some task was waiting for one 71.1% of
the time, and a job took 11.25 s against 6.74 s on one worker. The pool ran at 78.3 jobs a minute,
93% of the 83.8 at which every CPU would be busy at that cost per job. Contention had begun at 8
workers: the host 68.5% busy, a task waiting for a CPU 15.1% of the time, jobs 6% slower.

One cost here grows with the pool. Between jobs, a container worker looks for containers that dead
attempts left behind, every 5 s, and a container job outlasts that, so it looks after nearly every
job (63–91 times in a 72-job batch). The gap between a worker's publish and its next claim grew from
a median 9 ms on one worker to 125 ms on 16. The store calls in that gap take a few milliseconds, so
the rest is the worker listing its containers and networks through Docker, slower the busier the
daemon is. It cost 0.8% of the workers' time at 16.

### With the real model: the provider's rate limit

Replay leaves out the one limit a real batch hit first. In M16's real-model trial, 881.7 s of the
973.0 s batch (91%) went waiting on Groq's free-tier limit of 8,000 tokens a minute for that model
on that key, with one worker ([m16](m16-bench-harness.md)). The 18 tasks used 108,804 tokens, 6,045
a task, so that key allows about 1.3 tasks a minute, and no pool size changes that: more workers
only wait in parallel. Against a provider whose limit isn't the binding one, the recorded-latency
run above is the measurement that applies.

## What did not limit it

- **Postgres round trips.** A job's store calls — claim, start, heartbeats, publish — took 9–15 ms
  of a worker's time per job in every pool with a CPU to spare: 1.4–1.6% of a job's service time at
  full speed, 0.2–0.3% at the model's latency. From 1 to 4 workers at full speed they didn't grow
  (10.2, 10.5 and 9.7 ms), and the mean claim stayed at 3.0–3.1 ms. Postgres itself never used more
  than 2.6% of one CPU in any trial.
- **The scheduler.** There is no scheduler process; a worker's claim is one `SKIP LOCKED` query. The
  gap between a worker publishing one job and claiming its next, on Postgres's clock, stayed at a
  median 3.1–3.7 ms at full speed and 4.7–5.5 ms at the model's latency, at every pool size up to
  16, with jobs in the workers' processes. In containers the gap grows, for the Docker check
  described above, not the claim.

When store calls did slow down, the callers were starving, not the database. At 8 workers on 4 vCPUs
at full speed the mean claim took 8.1 ms and the publish-to-claim gap 9.9 ms, about three times what
they took at 4 workers, and a job's store calls 25 ms; at 16 workers in containers, 26 ms.
Postgres's CPU per job didn't move (4 ms at 4 and 8 workers): a worker waiting for a CPU waits
longer for every answer it reads.

## A bug the measurements found: batches were submitted before the workers were up

In the first runs ([m21-before-ready](results/bench/m21-before-ready/)), each 16-worker batch at the
model's latency opened with the host 97–99% busy for its first half second, and its first claims
spread over 0.21–0.71 s in one trial and 0.14–0.49 s in the other. The bench started the api and the
workers together, then submitted as soon as the api answered a health check, on the assumption that
the workers would be up by then.

A short experiment ([startup](results/bench/m21/startup/), on the first VM at `c8b8e6a`) started the
api and N workers the way the bench does, five times for each N, and timed both:

| Workers | Api first answers | Last worker up | Workers up after the api answered | Last one, after the api |
|---|---|---|---|---|
| 1 | 0.67 s | 0.73 s | 5 of 5 | 0.07 s [0.01–0.13] |
| 2 | 0.69 s | 0.76 s | 10 of 10 | 0.07 s [0.06–0.11] |
| 4 | 0.79 s | 0.99 s | 17 of 20 | 0.12 s [0.10–0.29] |
| 8 | 1.55 s | 1.79 s | 36 of 40 | 0.24 s [0.20–0.65] |
| 16 | 2.70 s | 3.39 s | 74 of 80 | 0.71 s [0.30–1.07] |

**142 of 155 workers came up after the moment the bench used to submit.** At 16 workers the last one
was up a median 0.71 s later, with 17 Python processes importing their code on 4 vCPUs. That
start-up landed inside the measured batch: it delayed those workers' first claims, and put their
start-up CPU in the batch's account.

The fix: a worker writes a ready file once it is up and about to poll, and the bench submits only
when every worker has. Every table in this document comes from runs made after it. The first runs,
and the same runs repeated with the fix on the same VM
([m21-previous-vm](results/bench/m21-previous-vm/)), show what it changed. With 8 workers on 4
vCPUs, the last worker's first claim came 0.56 s after the submit before the fix and 0.22 s after
it.

One change went the other way, and it is a cost of the design rather than of the bench. A worker
that is up and idle polls an empty queue with a backoff that grows from 50 ms to 500 ms, so when a
batch lands it can be up to half a second from its next look; a worker that had only just started
looked at once. With the fix, a one-worker batch's first claim came 0.12 s after the submit instead
of 0.06 s. An idle fleet pays that for polling instead of being told about new work, about 1% of a
12-second batch at 4 workers. `LISTEN/NOTIFY` is the candidate fix the design names; it stays a
candidate until it is A/B tested.

## What was built

- **A resource sampler** (`bench/resources.py`). From just before a batch is submitted until it is
  seen to be done, it reads `/proc/stat` every 0.5 s for the host's busy, idle and stolen time, and
  Linux's CPU pressure counter for how much of the time some runnable task waited for a CPU. At both
  ends it reads the CPU of each fleet process tree: the workers, the api, the Postgres server, the
  Docker daemons, and the bench itself. A tree's CPU is its processes' own time plus what the kernel
  folds in from the children they waited for, so the sandboxed commands a worker runs count towards
  it even after they exit. Its tests burn a known amount of CPU in a child that is waited for and
  one that is still running, and check the tree's count.
- **Store-call timing in the worker.** Every store call is timed by kind, and `--stats-out` writes
  the counts and seconds when the worker exits. A claim that finds nothing is counted apart from one
  that gets a job, so an idle worker's polling stays out of the per-job figures.
- **Records carry both** (`TrialRecord.resources`, `TrialRecord.database`), and summaries take
  medians and ranges of them like any other metric.
- **`bench.cli scale`**: one fleet per worker count on one Postgres, every pool's trials interleaved
  and their order reversed every other trial. The A/B is now the two-arm case of the same code. CI
  runs it on every push at 1 and 2 workers, and fails on any replay divergence.
- **Ready files** for workers, and the bench waiting for them (above).
- **[analysis.py](results/bench/m21/analysis.py)**, which recomputes every number in this document
  from the records, with the standard library alone.

## Honest notes

- **One machine, shared by everything.** The workers, the api, Postgres and the bench share 4 vCPUs.
  At full speed the workers' own CPU was 98% of the host's busy time, so the rest took little, but
  on separate hosts each would have its own CPUs, and nothing here measures a network between them.
- **The machine's speed moves.** The container running this session was restarted between the first
  runs and these, onto a VM with the same CPU model and a newer kernel build. On the first VM the
  same 4-worker configuration at full speed ran at 366.6 tasks a minute in one run and 419.7 in the
  next, 20 minutes later, using 0.62 and then 0.54 CPU-seconds a job as the VM's stolen time halved
  ([m21-previous-vm](results/bench/m21-previous-vm/)). On this one, the two runs agreed within 2%
  (336.6 and 331.0). So no comparison here crosses runs: within a run, the interleaving spreads any
  drift over every pool, and speedups pair trials from the same round.
- **Two runs go beyond the plan.** The plan was 1, 2 and 4 workers at full speed, 1–16 at the
  model's latency, and 1, 2 and 4 in containers. The run of 4 against 8 workers at full speed was
  added after the first full-speed run, to see the CPU ceiling rather than predict it, and the
  container run went to 8 and 16 to reach the point where containers stop scaling.
- **A batch has an end.** A fixed batch pays for its last jobs running on a part of the pool; a
  steady stream wouldn't. That cost is reported as it was measured, not removed.
- **CPU accounting has gaps.** Process CPU is read in 10 ms ticks per process. Processes that leave
  their tree before they exit — a container's shim, `runc` — count only towards "other", with the
  kernel's own threads. The api's CPU is mostly the bench polling it every 0.1 s.
- **Replay is not a real model.** With the real model on M16's key, the provider's rate limit sets
  the ceiling, as above.

## Reproduce

```bash
docs/results/bench/m21/run.sh idle         # what the host does with nothing of the bench running
docs/results/bench/m21/run.sh zero         # 1, 2, 4 workers at zero latency, 5 trials each
docs/results/bench/m21/run.sh past-cores   # 4 and 8 workers at zero latency
docs/results/bench/m21/run.sh recorded     # 1, 2, 4, 8, 16 workers at recorded latency
docs/results/bench/m21/run.sh containers   # 1, 2, 4, 8, 16 workers in containers, 3 trials each
python3 docs/results/bench/m21/analysis.py # every number in this document

cd backend
uv run python -m bench.cli scale --workers 1 2 4 --tasks 72 --trials 5    # on your own machine
```

The scale command starts a throwaway local Postgres unless given `--database-url`, and needs the
task image (`scripts/build-task-image.sh`) for `--execution container`.
