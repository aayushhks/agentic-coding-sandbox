# M19 — Controlled Execution: Containers, Limits, Egress Grants and Cancellation

Each attempt can now run in a locked-down container of its own, under its batch's execution
policy. The container runtime enforces the CPU, memory, process and scratch-space limits, the
worker enforces the wall clock, there is no network unless a destination is granted, and the only
writable path is scratch space. Inside, the agent's sandbox still puts every command it runs in
namespaces of its own. A running job can be cancelled, and its lease is released as soon as its
worker stops it. The policy model — what is denied by default, what can be granted and by whom, and
exactly what a task can reach — is in
[design.md](design.md#controlled-execution-the-policy-model).

## What was built

- **Execution policies** (`fleet/policy.py`, migration 0003). A batch asks for CPUs, memory,
  processes, scratch space, a timeout and egress destinations. The API refuses, with a 403, any
  policy over the operator's ceilings or naming a destination the operator hasn't made grantable.
  The policy is stored with each job. Each attempt records the policy it ran under, the image, how
  it exited, whether it was killed for memory or time, and what it used.
- **A container per attempt** (`fleet/execution.py`, `fleet/containers.py`, `fleet/docker.py`).
  With `--execution container`, a worker runs each attempt in a fresh container with the policy's
  limits: read-only root, the job mounted read-only, `/tmp` the only writable path, no network,
  user 10001 with no capabilities, `no-new-privileges`, and Docker's default seccomp profile plus
  `unshare`. The worker talks to the Docker Engine API over its socket. Containers are labelled
  with their job, attempt and worker, and a worker removes any whose attempt no longer holds a
  lease, so a dead worker's container doesn't outlive its lease by long.
- **The task's entrypoint** (`fleet/task.py`) runs the job and reports progress, resource use and
  the result on its own stdout, as lines tagged with a per-attempt token that nothing else in the
  container can read. How and why is in the design doc.
- **Egress grants** (`fleet/egress.py`): for each attempt granted egress, an internal network and
  a proxy that forwards `CONNECT` for exactly the granted `host:port` destinations.
- **Timeouts that keep partial work.** The agent loop reports each step as it is recorded
  (`fleet/progress.py`), so a task stopped at its deadline is published as `failed` with `timeout`,
  the steps it had reported and the tail of its logs. A memory kill is published the same way, with
  `memory_limit`. Neither is retried.
- **Cancellation** (`POST /jobs/{id}/cancel`): a queued job at once; a running one at its worker's
  next heartbeat, which stops the run, removes the container and releases the lease. The invariant
  checker learned that a cancel wins over any result that hadn't landed.
- **The sandbox inside a container** (`app/sandbox/`). Commands now get their own PID namespace as
  well as their own network namespace, inside a user namespace when not running as root. With
  `SANDBOX_REQUIRE_ISOLATION` set, the sandbox refuses a command it can't isolate, saying why,
  instead of falling back to running it less isolated. Task containers set it.
- **Probe jobs** (`fleet/probes.py`) that try what a container should refuse, so the tests run
  real containers instead of inspecting configuration.
- **CI** builds the task image and runs every container test on each push; without Docker or
  namespaces they fail rather than skip.
- **Measurement**: `bench.cli ab --arms process-container` compares the fleet in process against
  containers, and two new commands measure where a container's time goes
  (`bench/container_overhead.py`) and how long a cancel takes (`bench/cancellation.py`).

## The tests

Each limit is tested by a job that tries to break it, in a real container
(`tests/test_fleet_container_execution.py`, 14 tests, all run in CI):

| Limit | The job | What the test checks |
|---|---|---|
| Memory | holds 400 MB under a 128 MB limit | killed by the kernel; `failed` with `memory_limit` on its first attempt, never retried; the attempt records the OOM kill |
| Wall clock | sleeps 30 s under a 3 s timeout, reporting a step every 0.1 s | stopped within 15 s; `failed` with `timeout`, keeping the steps it reported; no container left behind |
| CPU | spins for 2 s with 0.5 CPU | got between 0.3 and 0.6 of a core |
| No network granted | connects to `1.1.1.1:443` | refused: the container has no network, and no proxy is configured |
| One destination granted | connects to a granted and an ungranted server on a stand-in internet | the granted one answers through the proxy and not directly; the other is refused both ways (403 from the proxy) |
| Generated code | runs commands in the agent's sandbox, as the agent would | no network, even when its task is granted egress; can't read the environment or write to the stdout of PID 1 or of the task process, read the task process's memory, or signal it; can make namespaces but can't mount anything; can read its job but not change it |
| DNS on a granted task's network | a container looks up an outside name, on an internal network and on an open one | Docker answers it only on the open one, so DNS is no way around the proxy |
| PID 1 | leaves 5 orphaned processes behind | none left a zombie (by hand, with the job run directly as PID 1, the same probe counted 5) |
| Cancellation | a 60 s job, cancelled while running, with 0.2 s heartbeats | stopped within 10 s, not at its 30 s lease; the job `cancelled`, its lease released, no container left |
| A dead worker | the worker is killed mid-task | the next worker removes the orphaned container and runs the jobs; the invariants hold |
| A runner that raises | in its container, with a budget of one attempt | dead-lettered with its error |

The report channel has its own tests (`tests/test_fleet_task_entry.py`): a line without the
attempt's token is kept as log, however it is dressed up, and a task started without a token
refuses to run.

## What M19 found along the way

- **CI had never checked the sandbox's network isolation.** Before M19, every CI run skipped
  `test_network_is_blocked_when_isolated` (351 passed, 1 skipped). Ubuntu 24.04 restricts
  unprivileged user namespaces through AppArmor, so the non-root sandbox on the runner couldn't
  isolate anything, and the test skipped itself. The green check never covered isolation. CI now
  lifts the restriction, and the isolation and container tests fail rather than skip.
- **The task image was missing pytest**, a development dependency the agent's sandbox needs to run
  each task's tests. The first container A/B caught it: replayed outcomes diverged from the
  recordings. The image now installs it.
- **Docker's default AppArmor profile denies `mount`.** The first design returned results through a
  writable `/out` mount, and hid `/in` and `/out` from generated code with a mount namespace. Local
  runs passed, on a kernel without AppArmor. On GitHub's runners the sandbox couldn't create its
  mount namespace and refused to run, and the two container tests that use it failed. The channel
  was rebuilt on the task's stdout, which needs no mount at all
  ([design](design.md#how-results-come-back)).
- **Without an init process, the task is PID 1 and must reap.** Dropping Docker's init (it would
  have held the container's stdout and the token, readable by generated code) left orphaned
  processes as zombies, each holding one of the 256 process slots. The entrypoint now forks a
  reaper; the test above counts 0 zombies where there were 5.
- **One A/B was thrown away.** Files were edited while it ran, so two of its trials were recorded
  from a dirty checkout. The numbers below come from a rerun on a clean one.

## Measured

Everything below ran on one machine, from one clean checkout of commit `bbc8f6f`: a virtualized
4-vCPU Intel Xeon @ 2.80 GHz, 15.72 GiB RAM, Ubuntu 24.04.4, kernel 6.18.44, Python 3.13.12,
PostgreSQL 16.13, Docker 29.3.1 (runc, overlayfs, cgroup v1, its builtin seccomp profile, no
AppArmor), with Postgres, the API, the worker and its containers all on it
([docker.json](results/bench/m19/docker.json)). Containers ran under the default policy: 1 CPU,
1024 MB, 256 processes, 512 MB of `/tmp`, 600 s. Records: [results/bench/m19/](results/bench/m19/);
`python3 docs/results/bench/m19/analysis.py` recomputes every number from them. In the container
arms, a record's `sandbox_config` describes the bench's own process; the sandbox inside each
container had the same limits and also required isolation.

### In process against in containers

One worker, the 18-task set replayed with seed 1, 5 trials per arm, interleaved with the arm order
alternating; once with the model answering instantly (zero latency) and once at each response's
recorded latency (records: [results/bench/m19/](results/bench/m19/)):

| Median [min–max] | In process | In containers |
|---|---|---|
| Batch wall clock, zero latency | 14.45 s [13.47–14.73] | **48.96 s** [46.36–50.07] |
| Batch wall clock, recorded latency | 91.90 s [91.81–92.52] | **127.09 s** [126.67–127.54] |
| Job service time p50 / p95, zero latency | 0.82 / 1.21 s | 2.77 / 3.45 s |
| Job service time p50 / p95, recorded latency | 4.71 / 9.99 s | 6.89 / 11.96 s |
| Outcomes, every trial | 15 solved, 2 escalated, 1 expected failure | the same, job for job |

**Containers add about two seconds to every job, whatever the job does**: +1.93 s at zero latency
and +1.96 s at recorded latency (the median over the 18 tasks of the difference in each task's
median service time). With the model answering instantly that made the batch **3.42× slower**
(3.15–3.64× across trial pairs, +35.38 s per batch); at the model's recorded latency, **1.38×**
(1.37–1.39×, +34.77 s). Every job's outcome matched the in-process arm's in every trial, with no
replay divergence: the container changed how long the agent took, not what it did.

### Where a container's extra time goes

Almost all of it is inside the container. At zero latency, of a container job's 2.75 s median
service time the container was running for 2.66 s; creating it, reading its output and removing it
took 82 ms (70–98 ms) around that.

The overhead probes split the container's own time. Each round ran the same two probe jobs through
a worker in process and in a container, alternating which went first: one that does nothing, and
one that times what every agent task pays wherever it runs (10 rounds; 10 timings of each kind per
round) ([record](results/bench/m19/container-overhead.json)):

| Median | In process | In a container |
|---|---|---|
| A job that does nothing, from running to its result landing | 2 ms | 658 ms, of which 584 ms inside the container |
| Importing the agent's runner, in a fresh interpreter | 737 ms | 878 ms |
| `pytest --version`, in the agent's sandbox | 177 ms | 237 ms |
| Starting Python | 15 ms | 17 ms |
| `true`, in the agent's sandbox | 9.6 ms | 7.5 ms |

**The cost is start-up, paid on every job.** A worker running jobs in process imported the agent's
runner once, when it started; a container starts empty, so each job pays for the container, the
interpreter, and importing the runner and everything under it. A job that does nothing already
costs 0.66 s in a container, and importing the runner 0.88 s more there. The two overlap — both
import pydantic, for one — so they bound the 1.93 s rather than add up to it. Work inside the
container runs at close to the same speed: Python starts as fast, a sandboxed `true` is slightly
faster, and pytest's start-up, which every test run pays, is 60 ms slower. (The two sandboxes are
not configured alike: the in-process worker runs as root here, so its sandbox needs no user
namespace, while in a container it runs as user 10001 and creates one.) A pool of
pre-started containers, or a process that imports once and forks per job, would remove most of the
start-up; either is an optimization to A/B, not to assume.

### What the tasks used, against the defaults

The 90 container jobs of the zero-latency A/B, against the default policy:

| | Median [min–max] | Default limit |
|---|---|---|
| Peak memory of the task process | 52.5 MB [52.2–53.2] | 1024 MB for the whole container |
| Peak memory of its largest child process | 52.2 MB [51.9–52.8] | |
| CPU time per job | 1.93 s [0.44–2.98] | |
| CPU kept busy while the container ran | 0.73 of a core [0.38–0.80] | 1 core |
| Written to stdout per job | 1.4 KB [0.7–2.2] | 8 MB of log |
| How they ended | all exited 0; no memory kill, no timeout | 600 s |

The memory figures are per process, the peak of each; the container's total, which also counts
`/tmp`, was not recorded.

### Cancellation

One worker on the fleet's default 30 s lease, so heartbeats every 10 s, through the API, 20 rounds
per mode. Each round cancelled a running job at a seeded random point between 5% and 95% of its
first heartbeat interval, and a queued job behind it. Every time comes from Postgres's clock
([container](results/bench/m19/cancellation-container.json),
[process](results/bench/m19/cancellation-process.json) records):

| Median [min–max] | In containers | In process |
|---|---|---|
| Running job: from the cancel request to the job ended and its lease released | 5.34 s [1.07–9.57] | 5.25 s [0.99–9.48] |
| … of which, stopping the run after the heartbeat that saw the request | 99 ms [85–117] | 6 ms [5–10] |
| Queued job: the cancel's API round trip, which ends it | 5.2 ms [4.1–8.2] | 4.6 ms [3.7–7.4] |
| Invariant violations; containers left behind | 0; 0 | 0; — |

**A cancel waits for the next heartbeat, then takes a tenth of a second.** Where the request lands
in the 10 s heartbeat cycle decides almost all of the latency, so the median says more about where
the cancels were placed than about the system: the bound is one heartbeat interval plus the stop.
Stopping a container — killing it, removing it, ending the job — took 85–117 ms, against 5–10 ms
for a run in the worker's own process. Every running job ended `cancelled` on its first attempt
with its lease released, every queued job was cancelled before any worker claimed it, and the
invariant checker passed over all 80 jobs.

## Honest notes

- **One machine.** Postgres, the API, the worker and its containers share 4 vCPUs on a VM, with
  cgroup v1 and no AppArmor; GitHub's runners differ (they run Docker's AppArmor profile, which is
  how the mount problem surfaced). The container tests pass on both; the measurements are from this
  machine only.
- **Neither replay is a real run.** Real jobs also wait on rate limits — M16's real-model trial
  had a 45 s median service time — and the container's share of one is not measured here. What is
  measured is that its cost per job stayed the same when the model got slower.
- **The defaults were not tuned to this bench.** Its tasks peaked at 53 MB against a 1024 MB limit
  and printed at most a few kilobytes against an 8 MB log. The limits are sized for agent tasks in
  general, and nothing was tightened to fit these measurements.
- **The one-CPU default was not compared against more.** At zero latency the tasks kept a median
  0.73 of a core busy while their container ran; whether a second core would shorten them is not
  measured.
- **Egress has no measurement.** No bench task needs the network — replay answers the model calls —
  so the proxy's own latency and throughput are untested beyond the tests above.
- **A cancel waits for a heartbeat**: up to one heartbeat interval, 10 s at the default lease.
- **Containers share the host's kernel, and the worker holds the Docker socket**, which is root on
  the host. What the policy model doesn't protect against is listed in the
  [design doc](design.md#what-it-doesnt-protect-against).

## Reproduce

```bash
scripts/build-task-image.sh                                          # the task image
cd backend
uv run pytest tests/test_fleet_container_execution.py                # the limits, for real
uv run python -m bench.cli ab --arms process-container --trials 5    # the a/b
uv run python -m bench.container_overhead --out overhead.json        # where the time goes
uv run python -m bench.cancellation --execution container --out cancel.json
python3 ../docs/results/bench/m19/analysis.py                        # every number above
```

The bench commands start a throwaway local Postgres when no database is given (`--database-url`
to use your own), and need a Docker daemon and the task image.
