# Design: a durable execution platform for agent tasks

How batches of agent tasks are queued, run and recovered. This document grows with each milestone;
sections below cover what is built and measured so far.

## Shape of the system

- **Fleet** (`backend/fleet/`) is a job queue on Postgres. It knows nothing about agents: a job is a
  name plus a JSON payload, and a worker hands the payload to a pluggable *runner* and publishes
  what the runner returns.
- **Workers pull.** Any number of worker processes claim the oldest available job, run it, and
  publish the result. There is no dispatcher process holding state: **Postgres is the only state**,
  so any process can die at any moment and everything it was doing is recoverable from the database
  alone.
- **The agent runner** (`bench/runner.py`) is one such plugin. A job's payload is self-contained —
  the task spec plus, for replay, the recorded model responses — so a worker needs nothing from the
  repo, which is what containerized workers will need.
- **The control plane** (`fleet/api.py`) accepts batches and serves job status and results.

## Why Postgres and `SKIP LOCKED`

The store has to make three things atomic: taking a job, recording its result, and changing its
state. After any crash, the whole picture must be rebuildable from what was committed.

Postgres gives transactions, row locks and constraints, and `SELECT … FOR UPDATE SKIP LOCKED` lets
any number of claimers each take a different queued row without blocking on each other and without
a hand-rolled lock table:

```sql
update fleet_jobs set state = 'claimed', attempt = attempt + 1, worker_id = :worker,
       lease_expires_at = clock_timestamp() + make_interval(secs => :lease), ...
where id = (select id from fleet_jobs where state = 'queued' and available_at <= clock_timestamp()
            order by id limit 1 for update skip locked)
```

**Against Redis:** it is memory-first, with durability traded against `fsync` settings; an atomic
claim-plus-lease needs a Lua script; and constraints like "a finished job has exactly one result"
can't be declared, so results and job history would live somewhere else and have to be reconciled
after a crash.

**Against a message broker** (SQS, RabbitMQ, Kafka): delivery with visibility timeouts is close to
leasing, but job state and results would still live in a database, so acknowledging a message and
committing its result become two writes to two systems. Exactly-once publication then needs a
deduplication layer on top — the problem a single transactional store avoids.

**What this gives up:**

- **One primary's write throughput.** Every claim, start, heartbeat and publish is a row write, an
  index update and WAL. At some rate the primary saturates; the scaling milestone measures where.
- **Push.** Workers poll, backing off from 50 ms to 500 ms when idle. The one-worker A/B measured the
  cost: the first claim of a fresh batch landed a median 78 ms after submission. `LISTEN/NOTIFY` could
  cut this; it is a candidate optimization, to be A/B tested rather than assumed.
- **Vacuum pressure** from high-churn updates on a small hot table.
- **Connection limits:** each worker holds connections, so many workers need pooling.

## The job lifecycle

```
queued ──claim──▶ claimed ──start──▶ running ──publish──▶ succeeded | failed | escalated
  ▲                  │                  │
  │                  └────────┬─────────┘
  │                           │ infrastructure failure: the lease lapsed, or the runner raised
  │                           ▼
  └─── attempts left ─── retry budget ─── none left ───▶ dead_lettered
       (after a backoff)
```

Every claim adds a row to `fleet_attempts`, so a job's history is an append-only log. Each attempt
ends exactly one way: `published` (its result was accepted), `lease_expired` (its lease ran out and a
reaper took the job back), or `released` (its runner raised, and the worker gave the job back).

## Invariants the database enforces

Rather than trusting the code, the schema makes the dangerous states impossible:

- **A job is finished with a result exactly when it has a published result** (a check constraint
  pairing `succeeded`, `failed` and `escalated` with `result_id`); a dead letter has none.
- **A job holds a lease exactly while it is claimed or running.**
- **At most one result per job:** `fleet_results.job_id` is unique, so even a bug that let two
  attempts publish could not store two results.
- **One row per attempt:** `fleet_attempts` is keyed by `(job_id, attempt)`.
- **No attempt past the budget:** `attempt <= max_attempts`.
- **An attempt that ended says how:** `ended_at` and `ended_by` are set together.

## Leases, heartbeats and fencing

- **A claim is a lease**, 30 seconds by default. While the job runs, the worker extends it every
  third of the lease (10 s), so a lease only has to outlive a missed heartbeat or two, not the job.
  A dead worker's job comes back once its lease lapses, instead of after a timeout sized for the
  slowest job.
- **The attempt number is the fencing token.** Every claim increments it, and every write a worker
  makes — marking the job running, a heartbeat, giving it back, publishing its result — names its
  attempt and is accepted only if that attempt still owns the job **and its lease is live**, checked
  under the job's row lock. A lease is live while `lease_expires_at > clock_timestamp()`, on
  Postgres's clock; a worker's own clock is never consulted, so clock skew between workers can't
  matter.
- **A lapsed lease is enough to fence a worker out**, even before any reaper has taken the job back.
  Without this, whether a late result lands would depend on whether a reaper happened to run first.
  With it, the rule is deterministic: once the lease has run out, that attempt can no longer write.
  A heartbeat that finds its lease gone makes the worker stop the run; a result that finishes after
  its lease ran out is refused.
- **A result is stamped with the clock reading it was checked against**: the publish reads
  `clock_timestamp()` once, under the row lock, and uses that one value for both the lease check and
  `published_at`. So "published before its lease ran out" is exactly checkable afterwards.
- **Reaping** ends lapsed attempts as `lease_expired`. A busy worker reaps every 5 seconds and an
  idle one on every poll; reapers take the lapsed rows with `SKIP LOCKED`, so several of them split
  the work instead of queueing behind each other, and a reap and a publish of the same job still
  serialize on its row lock.

## Retries and dead letters: the policy

- **A runner reports what the task did, or raises.** A returned outcome — `succeeded`, `failed`
  (the agent couldn't solve it) or `escalated` — is final, and never retried: retrying a wrong
  answer doesn't make it right. A raised exception means something underneath the task failed — the
  model API, the sandbox, the runner itself — and so does a lapsed lease: the worker died, hung or
  was cut off.
- **Infrastructure failures are retried with a doubling backoff**: 2 s after the first, 4 s after
  the second, capped at 60 s. The job waits in `queued` with an `available_at` the claim respects,
  and any worker can take it after that.
- **Each batch sets a retry budget** (`max_attempts`, default 3). A job whose last allowed attempt
  ends without a result is **dead-lettered**: a final state, with no result and the last error
  recorded, so it stops consuming workers and shows up in the batch's counts.
- **What the budget gives up.** It counts lapsed leases, because a job that kills its worker (a
  poison pill) otherwise loops forever. The price is that a job long enough to be caught by repeated,
  unrelated worker failures can be dead-lettered through no fault of its own; the stress test sees
  this (see [m18](m18-worker-pool.md)). Separate budgets for lapsed leases and runner errors would
  soften it.
- **The replay runner never raises for a task's failure.** A replayed execution, including one that
  replays a recorded provider error, is published as final: replay reproduces it exactly, so a retry
  could only fail the same way.

## The invariant checker: what it proves and what it doesn't

`fleet/invariants.py` reads a finished run back from the database — the jobs, every attempt and
every result, in one repeatable-read snapshot — and reports every violation of four invariants:

| Invariant | Checked as |
|---|---|
| **Exactly one result per job** | every job finished with an outcome has exactly one result, the one it points at, with the same outcome; no other job has one |
| **Nothing lost** | every submitted job is in the store and in a final state |
| **No stale writes** | every result came from the job's last attempt, by the worker holding it, before that attempt's lease ran out, and closed that attempt; each attempt was claimed only after the one before it ended |
| **Accounting adds up** | each job's attempt log runs 1…n with no gaps and every attempt ended; a reaped lease had run out and a released one hadn't; only the last attempt may have published; a dead letter used its whole budget; the final states sum to the jobs submitted |

**What it proves**, for a run that has drained: across what Postgres recorded, no job has zero or two
results, none is stranded, no attempt overlapped another of the same job, and every accepted result
was written by the only attempt entitled to write it, while its lease was live.

**What it doesn't:**

- **Side effects of a refused run.** A worker that was fenced out still ran its job; whatever the
  job did outside the database happened. The checker proves the stale result never landed, not that
  the stale run did nothing. Containing what a run can touch is the controlled-execution milestone.
- **Attempts that left no trace.** A refused publish writes nothing, so the checker proves no stale
  write landed, not that none was tried; the workers count and log refusals separately.
- **The attempt log's own honesty.** It cross-checks three tables written by the same store code;
  a bug that corrupted all three consistently would pass. The database constraints are the second
  line.
- **A clock that runs backwards.** Every comparison uses Postgres's `clock_timestamp()`; a wall-clock
  step backwards during a run could make a correct run look wrong (never the reverse).
- **Anything mid-run.** It checks a drained run; "nothing lost" means nothing was left unfinished,
  not that anything finished quickly.

It can fail: its tests feed it a clean run and sixteen corruptions, one or more per invariant, and
check each is caught under the invariant it breaks.

## Idempotent submission

A batch may carry an idempotency key. The store hashes the batch's content, including its retry
budget; resubmitting the same key with the same content returns the original job ids, and the same
key with different content is refused. Two concurrent submissions with one key serialize on the
key's unique index, so exactly one batch is created.

## One clock

Every timestamp — submitted, claimed, started, heartbeat, published, finished — comes from
Postgres's clock (`clock_timestamp()`), so queue wait, service time and lease checks are comparable
across processes and, later, machines.

## Still to come

- The policy model for controlled execution (containers, limits, network) — M19.
- The full fault matrix around the checker: kills at each exact point, dropped database
  connections, API restarts — M20.
- Where scaling stops being linear, measured — M21.
- What would change at ten times the scale.
