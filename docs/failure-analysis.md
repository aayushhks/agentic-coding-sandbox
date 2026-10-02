# Failure Analysis: What Failed, and Whose Fault It Was

Every failure seen while building and measuring the platform with the real agent as its workload:
what happened, how it was found, whose fault it was, the evidence, and what was done about it. That
includes the ones that look bad — bugs in the platform, a test that passed for the wrong reason,
healthy jobs given up on, a benchmark that timed its own start-up, runs thrown away, and the
operator's own mistakes — because a list of only the failures that were fixed cleanly would describe
the list, not the system. Where a write-up already tells the story in full, this links to it.

## Whose fault

| Class | What it covers | Entries |
|---|---|---|
| **Agent** | the model or the agent's loop did the wrong thing on a platform that worked | [1–2](#agent) |
| **Platform** | the fleet — its queue, workers, leases and containers — did the wrong thing | [3–6](#platform) |
| **Harness** | the code that measures and tests the system was wrong, or so was the person running it | [7–14](#harness) |
| **Provider** | the model's API: its rate limit, its daily cap, the builds behind one name, a retired model | [15–18](#provider) |
| **Environment** | the machine, the VM, the CI runners and the network policy | [19–23](#environment) |

A failure with two causes is filed under the one whose fix removed it, and the other is named. Every
run thrown away is listed [at the end](#runs-thrown-away-and-runs-kept-though-superseded), whatever
its class.

## What it adds up to

- **The agent failed twice, both on Llama 3.3 70B in M6, and both times in how it finished, not in
  what it understood**: once it declared a task done without a test, once it lost a correct answer
  to one stray brace. Both fixes, in M7, are in the loop, not the model. On `qwen/qwen3.8-27b`
  since, every real-model job has met its expectation: 59 of 59 over four trials.
- **The platform's two real bugs were found by fault injection, not in use**, and each fix is
  guarded by a test that fails without it. Its sharpest trade-off showed under the stress test: a
  job can use up its retry budget through no fault of its own.
- **The harness had the most failures, and each would have misreported the system, not broken it**:
  a dirty flag on clean runs, a test that passed a broken build, a batch that timed its own
  start-up, a trial that one stopped job would have wiped out, two configurations stated wrongly.
  Each was found by a further check — a build broken on purpose, a measurement that looked wrong,
  reading the code before an expensive run — which is the case for having them.
- **The provider shaped every real-model result**: real batches spent 88–91% of their time waiting
  on its rate limit, its daily cap ended an experiment after one round, and one model name was
  answered by six or seven builds a trial.
- **The environment made timings hard to compare**: the VM's speed moved by up to 8% between
  sessions and by 14% within one, so every comparison here is interleaved within one run.

## Agent

### 1. `fix_binary_search`: the right fix, a new off-by-one, and no test before finishing (M6)

**What happened.** The task's binary search loops forever because one branch sets `lo = mid`. The
agent diagnosed the loop and fixed it, but "symmetrized" the other branch to `hi = mid - 1`, which
skips the boundary element, and the hidden tests failed: `assert -1 == 3`.

**Whose fault.** The agent's. It ran `run_tests`, got exit code 5 — "no tests ran", since the hidden
suite isn't there while it works, by design — read that as a pass, and finished. `add_numbers` did
exactly the same and passed only because its code happened to be right.

**Done.** Fixed in M7 by the verified-finish gate (`63c7e02`, `9acc200`): `finish` is refused until
a test the agent wrote has passed. The hardened run solved the task, writing its test before its
fix. The gate checks the agent's own tests, not the hidden ones, so a weak test can still let a
wrong solution through: it narrows this failure, it doesn't rule it out.

**Evidence.** [m6](m6-real-agent-run.md#failure-analysis),
[m7](m7-analysis.md#what-actually-changed-per-task), and the
[before](results/groq-llama-3.3-70b-v1.json) and [after](results/groq-llama-3.3-70b-v2.json)
records.

### 2. `lru_cache`: one stray brace, three rejected responses, nothing written (M6)

**What happened.** Three responses in a row were rejected as malformed ("Extra data" at characters
1083, 1077 and 650), the loop stopped at its cap of three, nothing had been written, and grading
failed with `ModuleNotFoundError`.

**Whose fault.** The agent's — the model and its loop together. Each response was a complete, valid
`write_file` call followed by one extra `}`. The loop's parser sliced from the first `{` to the last
`}`, which kept the extra brace, and at temperature 0 the model repeated the same output. It is
filed under the agent because the fix was in the loop.

**Done.** Fixed in M7 by a parser that reads the first balanced object (`e00693e`): the hardened run
had no malformed steps and solved the task. The two fixes together cost 17% more tokens over the
benchmark (65,400 to 76,645), most of it `lru_cache`, which now does the work instead of dying at
the start.

**Evidence.** [m6](m6-real-agent-run.md#failure-analysis),
[m7](m7-analysis.md#the-cost-of-hardening).

**Since the fixes.** On `qwen/qwen3.8-27b`, every real-model job has met its expectation: 59 of 59,
over [M16's trial](results/bench/sequential-real-qwen3.8-27b/) and the three
[M22](results/bench/m22/) trials that ran jobs — 15 solved, both tickets escalated and the one task
built to fail failing, in each complete trial, and 5 of 5 in the trial the daily cap stopped. That
is a different model on a different task set from M6's, so it shows the failures haven't recurred
here, not that they can't.

## Platform

### 3. A worker frozen mid-transaction held its job forever (M20)

**What happened.** A worker stopped for good in the middle of a claim's or a publish's transaction
kept its job's row lock. Claims and reaps take rows with `SKIP LOCKED`, so every one of them passed
that job by: it was never finished, and its batch never drained.

**Found by.** Fault injection, in the first runs of its two hang scenarios: 0 of 6 runs passed, at
`5abadb6`.

**Whose fault.** The platform's.

**Done.** Fixed: worker sessions set Postgres's `idle_in_transaction_session_timeout` to the lease,
so Postgres ends a frozen session and rolls its transaction back, and the job is claimed or reaped
like any other. With the fix, 20 of 20 runs pass; with only the fix undone, 0 of 4. A test holds a
job's row in an open transaction and checks that another worker can claim the job once the session
has been ended.

**Evidence.** [m20](m20-fault-injection.md#a-worker-frozen-mid-transaction-held-its-job-forever),
[the runs before the fix](results/chaos/m20-before-idle-timeout/).

### 4. The agent's grading starved its worker's heartbeats (M20)

**What happened.** The agent runners graded each task by running its hidden tests with a synchronous
`pytest` on the worker's event loop, so a worker couldn't send a heartbeat while it graded. On the
chaos scenarios' 1 s leases a lease could lapse mid-grading; the result was then refused and the job
ran again.

**Found by.** The first CI runs of the chaos job: in 5 of 12 agent-scenario runs a healthy job ended
with no result. The CI logs from then don't show how each attempt ended, so that lapses used up the
retry budget is the most likely reading, not one the logs show.

**Whose fault.** The platform's.

**Done.** Fixed: grading runs in a thread, as the agent's tool calls already did. In CI, 0 of 18
runs failed after the fix. On this machine, 20 runs a side: 10 late results refused with grading on
the loop, 0 with the fix. At the default 30 s lease the loop would have to block for 20 s to lose a
lease, so the bug needed short leases or slow grading to bite. **Still open:** writing a task's
files and probing the sandbox's isolation still run on the loop, and the probe can block for up to 5
s.

**Evidence.** [m20](m20-fault-injection.md#the-agents-grading-starved-its-workers-heartbeats), [the
A/B](results/chaos/m20-agent-grading/).

### 5. Healthy jobs were dead-lettered under heavy chaos (M18)

**What happened.** In the stress test — 300 jobs on 8 workers with 1 s leases, a worker killed or
paused every 0.3–0.8 s — 1–4 jobs per run that did nothing wrong were dead-lettered, 28 in ten runs.
Of the 9 inspected, 8 were long jobs, which are mid-job most often and so hit most often.

**Found by.** The stress test's per-kind checks.

**Whose fault.** The platform's design. One retry budget counts both lapsed leases and runner
errors, so a job whose worker is killed three times is given up on, through no fault of its own.
Counting lapsed leases is what stops a job that kills its own worker from looping forever.

**Done.** Kept, as specified, and reported as a negative result. Separate budgets for lapsed leases
and runner errors, or a larger one for lapsed leases, would soften it. The chaos is far harsher than
a real fleet's: 25–32 kills and pauses in a 19-second run.

**Evidence.** [m18](m18-worker-pool.md#results).

### 6. Three container problems found while building them (M19)

- **The task image lacked pytest**, which the agent's sandbox needs to run tests. The first
  container A/B caught it: replayed outcomes diverged from their recordings. The image now installs
  it.
- **Docker's default AppArmor profile denies `mount`.** The first result channel, a writable `/out`
  mount hidden from generated code by a mount namespace, passed locally on a kernel without AppArmor
  and failed two container tests on GitHub's runners. The channel was rebuilt on the task's stdout,
  which needs no mount.
- **Without Docker's init process, the task is PID 1 and must reap.** Orphaned processes became
  zombies, each holding one of the container's 256 process slots: 5 in the test. The entrypoint now
  forks a reaper, and the test counts 0.

**Whose fault.** The platform's. **Done.** All three fixed, each with a test. **Evidence.**
[m19](m19-controlled-execution.md#what-m19-found-along-the-way).

**Still open on the platform:**

- **Dead letters stay dead.** No API requeues one.
- **A refused run still ran.** Fencing keeps a stale result out of the database; it can't undo what
  the run did elsewhere. In a container with no network granted, there is little else for it to
  touch.
- **The Docker socket is root on the host**, so a worker that runs containers holds that power
  ([design](design.md#what-it-doesnt-protect-against)).

## Harness

### 7. The dirty flag counted the bench's own output (M16)

**What happened.** Every record says whether the checkout differed from its commit, and the records
of a run's earlier trials were untracked files, so 9 of the first 10 replay trials said "dirty" with
no code changed.

**Done.** Fixed in `578f038`: the bench's output directory is left out, its inputs aren't. The
affected records are kept as [superseded](results/bench/superseded/), and both replay baselines were
run again on a clean checkout. **Evidence.**
[m16](m16-bench-harness.md#bugs-found-while-building-it).

### 8. A chaos test passed a build with fencing removed (M20)

**What happened.** `publish-before-stop` paused a worker after its job ran and woke it past its
lease. A build in which a publish no longer checked which attempt it was for passed it too, in both
runs. The scenario's three workers were all armed to pause, so while they were paused no worker was
free to take a job over, and a late publish was refused by the other checks: fencing was never
exercised.

**Found by.** Running each property's scenarios against a build broken on purpose.

**Done.** Fixed: the scenario wakes a worker only once a later attempt holds its job, and two
unarmed workers are free to take it. The same broken build now fails it in 5 of 5 runs, with 120
violations. **Evidence.** [m20](m20-fault-injection.md#a-test-that-passed-for-the-wrong-reason),
[the first try](results/chaos/m20-mutations-first-try/).

### 9. The fault matrix catches a lost claim lock only sometimes (M20)

**What happened.** Of five bugs planted on purpose, the matrix caught four in every run, but a claim
that no longer locks its row in only 3 of 10 runs: a double claim needs two workers claiming in the
same instant. And one bug — a publish that no longer recognised its own committed result — was
caught only by the checks of each fault's effects; the invariants alone would have missed it.

**Done.** Open, and stated: the store's concurrency test, three claimers draining one queue, catches
the claim-lock bug in 5 of 5 runs and runs on every push. **Evidence.**
[m20](m20-fault-injection.md#is-the-harness-real), [the broken
builds](results/chaos/m20-mutations/).

### 10. Three CI runs failed because a planned fault never happened (M20)

**What happened.** A dropped connection meant for a heartbeat was armed on jobs too short to send
one; and, as the code and the fix suggest — no logs from that run survive — a freshly started,
unarmed worker did the reaping an armed one was waiting to do.

**Done.** Fixed in the harness: longer jobs, workers armed for their first or second hit, and a
killed reaper replaced only once its faults are spent. The platform was at fault in none of them;
each is the "every planned fault happened" check working. **Evidence.**
[m20](m20-fault-injection.md#honest-notes).

### 11. The bench submitted batches before its workers were up (M21)

**What happened.** The bench started the api and the workers together and submitted as soon as the
api answered, assuming the workers would be up. 142 of 155 worker starts in a timing experiment came
up after that moment, up to about 1 s late at 16 workers, so their start-up landed inside the
measured batch: 16-worker batches at the model's latency opened with the host 97–99% busy for their
first half second.

**Found by.** Measurements that looked wrong: a 16-worker batch's first claims spread over up to
half a second.

**Done.** Fixed: a worker writes a ready file once it is about to poll, and the bench submits only
when every worker has. At 8 workers the last first claim moved from 0.56 s to 0.22 s after the
submit. Every M21 table comes from runs after the fix; the runs before it are
[kept](results/bench/m21-before-ready/). It exposed a cost of the design: an idle worker polls with
a backoff up to 0.5 s, so a one-worker batch's first claim came 0.12 s after its submit, not 0.06 s.
**Evidence.**
[m21](m21-scaling.md#a-bug-the-measurements-found-batches-were-submitted-before-the-workers-were-up),
[the experiment](results/bench/m21/startup/).

### 12. One stopped job would have lost a whole fleet trial (E1)

**What happened.** When the bench collected a fleet batch's results, a job the fleet had stopped at
a limit of its policy raised a validation error, and a dead-lettered job raised on purpose, so
neither the trial's record nor any of its recorded responses would have been written. No replayed
run had reached either state; a real-model run can, since four workers share one rate limit and a
job's 600 s deadline counts its time waiting on that limit.

**Found by.** Reading the result path before spending a day's token budget on the first real-model
run through the fleet. It never fired.

**Done.** Fixed before that run (`f7fc76f`): a stopped job is kept as a failure underneath the task,
with the tokens of the steps it reported and, since `db181ef`, their rate-limit waits; a
dead-lettered one is kept as a failure with its attempt history. A test runs a batch with one job
that hangs past its deadline and one whose every attempt crashes.

### 13. Interrupted records didn't say why (M22)

**What happened.** The trials the daily cap stopped recorded only that the cap was reached; the
provider's own message was read with a probe afterwards.

**Done.** Fixed in `ea58397`: records keep the provider's message. **Evidence.**
[m22](m22-records.md#honest-notes).

### 14. Two configurations stated wrongly: the report's fault matrix and M18's machine (E4, M18)

**What happened.** The report page described the fault matrix's runs as "each run 18 jobs on 3
worker processes", read from the first record alone: 230 of the 260 runs had 30 jobs on 3 workers,
20 had 18 on 3, and 10 had 18 on 5. And M18's write-up said its comparisons ran on "the same
machine" as M17's, an Intel Xeon @ 2.80 GHz, where every one of its 50 records names a Xeon @ 2.10
GHz.

**Found by.** Checking each number for the README's final pass against the records it came from.

**Done.** Both fixed (`ad957ff`, `f06c9f0`). The report states a configuration only where every
record behind a number agrees, refuses where they differ, and lists the fault matrix's three shapes
of run; a test checks it names every shape the records hold. M18's page names its records' CPU and
says it first said otherwise. The numbers were right in both; what they were measured on was
misstated. No M18 conclusion changes, since its comparison was made within one session, but the
session it found 10% faster than M17's had a different CPU model.

## Provider

### 15. The rate limit, not the platform, sets real-model throughput (M16, M21, M22)

Groq's free tier allows this key 8,000 tokens a minute on `qwen/qwen3.8-27b`. M16's one-worker real
batch spent 881.7 s of its 973.0 s (91%) waiting on that limit, and M22's two complete trials 90%
and 88%. At M16's 6,045 tokens a task that allows about 1.3 tasks a minute whatever the pool size,
so M21 measured scaling on replay and says so. **Evidence.**
[m16](m16-bench-harness.md#what-the-baseline-says),
[m21](m21-scaling.md#with-the-real-model-the-providers-rate-limit),
[m22](m22-records.md#the-demonstration-one-sentence-in-the-prompt).

### 16. The daily cap cut the prompt experiment to one round of three (M22)

Round 2's second arm stopped after 5 of its 18 jobs, and every trial after it was refused at once:
"Limit 200000, Used 199523, Requested 2513". The two missing rounds needed about 43 more hours of
budget. The interrupted trials are kept, marked and left out of the pairing, and the comparison's
intervals say what one round can: how the change varies across tasks, not how one task varies from
run to run. **Evidence.** [m22](m22-records.md#the-demonstration-one-sentence-in-the-prompt).

### 17. One model name, six or seven builds, and temperature 0 isn't deterministic (M22)

Every call now records the build that answered it. The baseline trial was answered by 6 builds and
the other arm's by 7; 33 of the 36 jobs were answered by more than one build, up to 6 in one job.
The unchanged prompt matched M16's run of it token for token on only 7 of 18 tasks, and differed by
up to 2.4 times on others (`lru_cache`: 8,507 tokens, then 20,042). **Evidence.**
[m22](m22-records.md#honest-notes).

### 18. The model behind M6 and M7 was retired (M16)

`llama-3.3-70b-versatile` now returns `model_not_found`, so 86.7% → 100% can't be rerun or compared
with anything since; M16 started a new baseline on another model. Before that, M7's earlier hardened
runs were thrown away because the free tier's rate limit injected provider errors into them
mid-benchmark ([below](#runs-thrown-away-and-runs-kept-though-superseded)). **Evidence.**
[m16](m16-bench-harness.md#the-real-model-trial), [m7](m7-analysis.md#honest-caveats).

## Environment

### 19. CI never ran the sandbox's network isolation test (found in M19)

Before M19, every CI run skipped `test_network_is_blocked_when_isolated` — 351 passed, 1 skipped —
because Ubuntu 24.04's AppArmor blocks the unprivileged user namespaces the sandbox needs without
root, and the test skipped itself. The green check never covered isolation. The runner's restriction
was the cause and a silent skip the harness's part; CI now lifts the restriction, and the isolation
and container tests fail rather than skip. **Evidence.**
[m19](m19-controlled-execution.md#what-m19-found-along-the-way).

### 20. A VM whose speed moves (M16, M18, M20, M21)

- Between sessions the VM's CPU model itself changed: the records name an Intel Xeon @ 2.80 GHz in
  M16, M17 and M19, and @ 2.10 GHz in M18 and M20–M22.
- Between sessions, the same zero-latency replay measured 12.38 s in one and 13.44 and 13.14 s in
  another: noise of about 8% ([m16](m16-bench-harness.md#determinism-and-how-noisy-the-clock-is)).
- M18's first A/B found the fleet 5% slower; build against build in one session, it wasn't
  ([m18](m18-worker-pool.md#did-m18-cost-anything-one-worker-measured-twice)).
- M20's first build comparison found the new code 0.62 s slower per batch (p = 0.0008), on a path it
  barely touched. A matched rerun found no difference; the cause is not settled
  ([m20](m20-fault-injection.md#did-m20-cost-anything)).
- Within one session, M21's first VM ran the same configuration at 366.6 tasks a minute and then
  419.7, twenty minutes apart, as its stolen CPU time halved ([m21](m21-scaling.md#honest-notes)).

So every comparison in these documents is interleaved within one run, and the deterministic measures
— outcomes, tokens, calls — come first.

### 21. The session's container was restarted, twice

The first restart came between M21's first runs and the rest and moved the work to another VM; every
M21 run was made again there, and the first VM's are [kept](results/bench/m21-previous-vm/). The
second came while the first real-model fleet run was waiting for the provider's daily budget, and
killed the waiting process before it had started anything; it was relaunched.

### 22. A Docker daemon nobody supervises

The session's Docker daemon stopped four times while the session was idle. Each time, local
container work failed with "connection refused" until the daemon was restarted by hand. No recorded
run was affected; GitHub's runners have their own.

### 23. The network policy keeps the price unverified (M16, M22)

This machine's network policy blocks Groq's pages, so the model's list price can't be read from its
source. M16's records carry `cost_usd: null` rather than an estimate; M22 prices tokens from a web
search's summary of the page, and [`bench/prices.json`](../backend/bench/prices.json) says so. The
key is on the free tier, which charges nothing.

## Runs thrown away, and runs kept though superseded

| Milestone | Runs | Why | Class | Instead |
|---|---|---|---|---|
| M7 | earlier hardened benchmark runs, thrown away | the rate limit injected provider errors mid-benchmark, which count against the solve rate | provider | one complete run with no provider errors |
| M16 | the first replay trials, kept as [superseded](results/bench/superseded/) | 9 of 10 flagged dirty by the bench's own output ([7](#7-the-dirty-flag-counted-the-benchs-own-output-m16)) | harness | both baselines run again on a clean checkout |
| M18 | the first build-against-build runs, thrown away | a fresh virtualenv had no cached bytecode, and the sandbox runs Python with `PYTHONDONTWRITEBYTECODE=1`, so every task took twice as long in both arms | environment | both builds on one interpreter and package set |
| M19 | one A/B, thrown away | files were edited while it ran, so two of its trials came from a dirty checkout | harness (the operator) | the same A/B on a clean checkout |
| M21 | a container run, deleted unread | its script was edited while bash was still reading it, which launched a run nobody meant, on a stale task image | harness (the operator) | none needed; run scripts are now read whole before they start, so editing one mid-run changes nothing |
| M21 | the first runs, kept as [before-ready](results/bench/m21-before-ready/) | submitted before the workers were up ([11](#11-the-bench-submitted-batches-before-its-workers-were-up-m21)) | harness | the same runs after the fix |
| M21 | the fixed runs on the first VM, kept as [previous-vm](results/bench/m21-previous-vm/) | the container restart moved the session to another VM ([20](#21-the-sessions-container-was-restarted-twice)) | environment | every run made again on one VM |
| M22 | four trials stopped by the daily cap, kept and marked interrupted | the provider's daily token cap ([15](#16-the-daily-cap-cut-the-prompt-experiment-to-one-round-of-three-m22)) | provider | left out of the pairing, named in the comparison |
