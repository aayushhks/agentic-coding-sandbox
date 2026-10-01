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
  repo to run it.
- **Each attempt runs in a container of its own** (`--execution container`), created for the
  attempt under the job's execution policy and removed after it, or, for tests and comparison, in
  the worker's own process. Either way the agent's sandbox puts every command it runs in namespaces
  of its own, so code the agent generates sits behind two fences.
- **The control plane** (`fleet/api.py`) accepts batches, checks each batch's policy against the
  operator's limits, cancels jobs, and serves job status and results.

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
  ▲  │               │                  │
  │  │               └────────┬─────────┘
  │  │                        │ infrastructure failure: the lease lapsed, or the runner raised
  │  │                        ▼
  └──┼── attempts left ── retry budget ── none left ──▶ dead_lettered
     │   (after a backoff)
     └──cancel──▶ cancelled ◀── cancel requested, seen at the next heartbeat (or at the failure)
```

Every claim adds a row to `fleet_attempts`, so a job's history is an append-only log. Each attempt
ends exactly one way: `published` (its result was accepted), `lease_expired` (its lease ran out and a
reaper took the job back), `released` (its runner raised, and the worker gave the job back), or
`cancelled` (its worker stopped it because a cancel was requested).

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

**What it proves**, for a run that has drained: across what Postgres recorded, every job that finished
with an outcome has exactly one result and no other job has any, none is stranded, no attempt
overlapped another of the same job, and every accepted result was written by the only attempt
entitled to write it, while its lease was live.

**What it doesn't:**

- **Side effects of a refused run.** A worker that was fenced out still ran its job; whatever the
  job did outside the database happened. The checker proves the stale result never landed, not that
  the stale run did nothing. Containing what a run can touch is the controlled-execution milestone.
- **Attempts that left no trace.** A refused publish writes nothing, so the checker proves no stale
  write landed, not that none was tried; the workers count and log refusals separately.
- **The attempt log's own honesty.** It cross-checks three tables written by the same store code;
  a bug that corrupted all three consistently would pass. The database constraints are the second
  line.
- **A clock that runs backwards.** Every comparison uses Postgres's `clock_timestamp()`. A wall-clock
  step backwards during a run could make a correct run look wrong, or stamp a late write early
  enough to hide it.
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

## Cancellation

- **A queued job is cancelled in the request's own transaction**, so no worker ever sees it.
- **A running job gets a cancel request**, stamped on its row. Its worker learns of it from the
  answer to its next heartbeat — every third of the lease, 10 s by default — stops the run (in a
  container: kills and removes it), and ends the job `cancelled`, **releasing the lease at once**
  rather than letting it lapse. Measured with containers at the default lease, a cancel ended a
  running job a median 5.34 s after the request and at most 9.57 s in 20 rounds, of which stopping
  the container took 85–117 ms after the heartbeat; a queued job was gone in a 5 ms round trip
  ([m19](m19-controlled-execution.md#cancellation)).
- **A cancel wins over anything that hadn't landed yet.** A publish that finds a cancel request
  ends the job cancelled instead of storing the result; an attempt reaped or released after a cancel
  request ends the job cancelled instead of retrying it.
- **What noticing at heartbeat cadence costs**: up to one heartbeat interval of work nobody wants,
  in exchange for no extra query per running job. Checking more often, or `LISTEN/NOTIFY`, would
  shorten it; both are candidates to measure, not to assume.

## Controlled execution: the policy model

Everything a task could use is denied unless granted. Each batch carries an execution policy, fixed
at submission and stored with each job; each attempt records the policy it ran under, beside the
image it ran, how it exited, whether it was killed for memory or time, and what it used.

### Who decides what

| Who | Decides | How |
|---|---|---|
| **The operator** | the most any batch may ask for, and the only destinations that may ever be granted | `FLEET_MAX_CPUS`, `FLEET_MAX_MEMORY_MB`, `FLEET_MAX_PIDS`, `FLEET_MAX_TMP_MB`, `FLEET_MAX_TIMEOUT_SECONDS` and `FLEET_GRANTABLE_EGRESS` (comma-separated `host:port` pairs) on the API; nothing is grantable unless listed |
| **The submitter** | each batch's policy, within those ceilings | `policy` on `POST /batches`; the API refuses a policy over any ceiling, or naming a destination that isn't grantable, with a 403 that names every violation |
| **The worker** | nothing | it enforces the stored policy through the container runtime, and records what it applied |

### Denied by default

| | Default | Enforced by | At the limit |
|---|---|---|---|
| CPU | 1 core | the CPU cgroup's quota | slowed, never killed |
| Memory | 1024 MB, no swap | the memory cgroup | the kernel kills a process; a task left with no result is `failed` with `memory_limit`, final |
| Processes | 256 | the pids cgroup | `fork` fails |
| Scratch space | 512 MB of `/tmp`, the only writable path, where the agent's workspace lives | a tmpfs, whose pages also count as the task's memory | writes fail |
| Wall clock | 600 s | the worker's timer, then a kill; 30 s later the container also ends itself, in case its worker is gone | `failed` with `timeout`, final, keeping the progress reported so far and the tail of the logs |
| Network | none: a network namespace with only loopback | Docker's `none` network | connections fail at once |
| Files | the image, read-only; the job at `/in`, read-only | a read-only root filesystem and a read-only bind mount | writes fail |
| Privilege | user 10001, no capabilities, no privilege gain | `CapDrop ALL`, `no-new-privileges`, Docker's default seccomp profile plus `unshare` | — |

A grant can raise any limit up to its ceiling and name `host:port` destinations to reach. Memory
and timeout kills are final failures, never retried: the same task would hit the same limit again.

`unshare` is the one system call added to Docker's default seccomp profile, which otherwise allows
it only with `CAP_SYS_ADMIN`. It lets the agent's sandbox, running without capabilities, create a
user namespace and the network and PID namespaces inside it. `mount` stays denied, by seccomp and
by Docker's default AppArmor profile; a test checks that generated code can make namespaces but
can't mount anything.

### How egress is granted

A batch granted egress gets, for each attempt, an internal Docker network with no route out. The
only other container on it is a proxy, which also has a leg on the outside network. The proxy speaks
only HTTP `CONNECT`, the way HTTPS clients tunnel: a `CONNECT` to exactly a granted `host:port` is
spliced through, any other destination gets a 403, any other method a 405. The task finds the proxy
through `HTTPS_PROXY`. A test checks that a granted destination is reachable through the proxy and
not directly, and that an ungranted one is refused both ways.

### What a task can reach

- **With no grant:** its own loopback, nothing else on any network. On disk: the image and its job,
  read-only, and its own `/tmp`. Its own processes, in its own PID namespace. Not the Docker socket,
  not the host's files, not another task.
- **With a grant:** also each granted `host:port`, through the proxy — whatever address that name
  resolves to from the proxy, when the connection is made. The task's own lookups answer only for
  its own network, which holds just the proxy: Docker doesn't resolve outside names on an internal
  network, so DNS is no way around the proxy either. The design relies on that, so a test checks it.
- **Code the agent generates**, run by the sandbox inside the container: no network at all, even
  when the task has a grant, since its own network namespace has only loopback. It cannot see or
  signal the task's processes, or read the task process's memory, environment or open files, so it
  can neither learn the report token nor write to the container's output. It can make namespaces
  of its own but can't mount anything. It can read the job at `/in` — by design, as the job is what
  the agent is working on — but not change it.

### How results come back

The task process reports on its own stdout: progress, resource use and the result, each a JSON line
carrying a token the worker generates for the attempt and passes in the container's environment.
The worker takes only lines carrying that token; anything else on stdout is kept as log. The task
process is the container's PID 1. Before anything else runs, it makes itself non-dumpable, which
leaves `/proc/1` owned by root, so nothing else in the container can read its memory, environment or
open files, and it removes the token from its environment, so nothing it starts inherits it. As
PID 1, it forks once: the parent only reaps the processes orphaned onto it and passes on the child's
exit code, and the child runs the job.

The first design wrote results to a writable `/out` mount instead, and kept generated code away from
it by giving sandboxed commands a mount namespace with `/in` and `/out` covered over. Docker's default
AppArmor profile denies `mount`, so on GitHub's runners the sandbox couldn't create that namespace
and refused to run. The stdout channel needs no mount, and leaves nothing writable that the worker
reads.

### What it doesn't protect against

- **A shared kernel.** Containers and namespaces share the host's kernel; a kernel exploit gets past
  both fences. A user-space kernel (gVisor) or a microVM per task would be the next step, at a cost
  to be measured.
- **The worker.** It holds the Docker socket, which is root on the host. Workers are trusted; tasks
  are not.
- **Names, not addresses.** A granted name is resolved by the proxy when the task connects, so
  whoever controls that name's DNS controls where the connection goes.
- **Granted egress is a way out.** A task can send its job, and anything it computes from it, to a
  granted destination.
- **Shared and missing limits.** Scratch space counts against memory; disk I/O is not limited; the
  container's log keeps the last 8 MB, so a task that prints more loses its earliest output, and
  one whose result line doesn't survive fails as an infrastructure error.
- **What a stopped run already did.** A run that was cancelled, timed out or fenced out still did
  whatever it did through a granted destination before it was stopped.

## Still to come

- The full fault matrix around the checker: kills at each exact point, dropped database
  connections, API restarts — M20.
- Where scaling stops being linear, measured — M21.
- What would change at ten times the scale.
