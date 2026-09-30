# Design: a durable execution platform for agent tasks

How batches of agent tasks are queued, run and recovered. This document grows with each milestone;
sections below cover what is built and measured so far.

## Shape of the system

- **Fleet** (`backend/fleet/`) is a job queue on Postgres. It knows nothing about agents: a job is a
  name plus a JSON payload, and a worker hands the payload to a pluggable *runner* and publishes
  what the runner returns.
- **Workers pull.** Each worker claims the oldest queued job, runs it, and publishes the result.
  There is no dispatcher process holding state: **Postgres is the only state**, so any process can
  die at any moment and everything it was doing is recoverable from the database alone.
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
where id = (select id from fleet_jobs where state = 'queued'
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

- **One primary's write throughput.** Every claim, start and publish is a row write, an index update
  and WAL. At some rate the primary saturates; the scaling milestone measures where.
- **Push.** Workers poll, backing off from 50 ms to 500 ms when idle. The one-worker A/B measured the
  cost: the first claim of a fresh batch landed a median 78 ms after submission. `LISTEN/NOTIFY` could
  cut this; it is a candidate optimization, to be A/B tested rather than assumed.
- **Vacuum pressure** from high-churn updates on a small hot table.
- **Connection limits:** each worker holds connections, so many workers need pooling.

## The job lifecycle

```
queued ──claim──▶ claimed ──start──▶ running ──publish──▶ succeeded | failed | escalated
   ▲                  │                  │
   └──── lease lapsed: reaped back to the queue, attempt closed as lease_expired
```

Every claim adds a row to `fleet_attempts`, so a job's history is an append-only log: attempt 1
claimed by one worker and lost when its lease lapsed, attempt 2 claimed by another and published.

## Invariants the database enforces

Rather than trusting the code, the schema makes the dangerous states impossible:

- **A job is finished exactly when it has a published result** (a check constraint pairing the
  final states with `result_id`).
- **A job holds a lease exactly while it is claimed or running.**
- **At most one result per job:** `fleet_results.job_id` is unique, so even a bug that let two
  attempts publish could not store two results.
- **One row per attempt:** `fleet_attempts` is keyed by `(job_id, attempt)`.

## Leases and publishing (as of M17)

- **A claim is a lease.** It lasts a fixed time (10 minutes by default, longer than the slowest
  real-model job measured, 180 s). A worker reaps lapsed leases when it finds the queue empty and every
  5 seconds while busy, so an abandoned job comes back without adding work to every claim.
- **Publishing is owned by an attempt.** The result, the final state and the attempt's closing are
  one transaction, and it only succeeds if the job is still on that attempt — checked under a row lock,
  so a reap and a publish of the same job serialize. An attempt whose job was reaped and re-claimed can
  no longer publish.
- **Not yet:** heartbeats that extend a lease while work continues (so leases can be short), and
  rejecting a publish whose lease has expired even if no one has reaped it yet (fencing). Both come
  with multiple workers in M18; until then a single worker never has its own running job reaped.

## Idempotent submission

A batch may carry an idempotency key. The store hashes the batch's content; resubmitting the same
key with the same content returns the original job ids, and the same key with different content is
refused. Two concurrent submissions with one key serialize on the key's unique index, so exactly
one batch is created.

## One clock

Every timestamp — submitted, claimed, started, finished — comes from Postgres's clock
(`clock_timestamp()`), so queue wait and service time are comparable across processes and, later,
machines.

## Still to come

- The policy model for controlled execution (containers, limits, network) — M19.
- The invariant checker, and what it proves and does not — M20.
- Where scaling stops being linear, measured — M21.
- What would change at ten times the scale.
