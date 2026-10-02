# Docs

Write-ups for the milestones that produced a result or an artifact worth explaining. The
earlier milestones (M1–M5: scaffold, sandbox, agent loop, benchmark, eval harness) are
described in the top-level [README](../README.md); the documents below cover the runs, the
analysis, and the systems built on top of them.

| Doc | What it covers |
|---|---|
| [m6-real-agent-run.md](m6-real-agent-run.md) | First full benchmark on a real model (Llama 3.3 70B via Groq) — **86.7% (13/15)** and a failure analysis |
| [m7-analysis.md](m7-analysis.md) | Two targeted hardening fixes taking the benchmark to **100% (15/15)**, with the v1→v2 diff, figures, and trace-level evidence |
| [m8-dashboard.md](m8-dashboard.md) | The read API + React dashboard over the persisted runs (solve rates, traces, interactive diff) |
| [m9-ci-eval-gate.md](m9-ci-eval-gate.md) | The regression gate that fails CI when a run regresses against the committed baseline |
| [m10-deploy.md](m10-deploy.md) | Packaging the API + dashboard as one Docker image and the compose stack |
| [m11-mcp-layer.md](m11-mcp-layer.md) | Exposing the agent's tools and an issue tracker as two MCP servers, with a swappable in-process / MCP transport |
| [m12-messy-input-hardening.md](m12-messy-input-hardening.md) | Escalation, prompt-injection defense (with a canary check), a messy-ticket dataset, and the ticket-resolution runner |
| [m13-production-readiness-eval.md](m13-production-readiness-eval.md) | Reframing the eval for a deployment owner: resolution / escalation / false-fix / injection-resistance rates and cost + latency (p50/p95), with a JSON report and regression diff |
| [m14-deployment-report.md](m14-deployment-report.md) | A stakeholder-framed "deployment report" dashboard view over the ticket-eval JSON — headline metrics, per-ticket outcomes, and inline trace drill-down |
| [m15-deployment-and-framing.md](m15-deployment-and-framing.md) | The README reframe around the deployment story, a recorded MCP client session, and deploy packaging (report baked into the image + static-exported) |
| [mcp-session.md](mcp-session.md) | A real recorded MCP client ↔ sandbox-server session (the screenshot-free demo) |
| [m16-bench-harness.md](m16-bench-harness.md) | The execution-platform baseline: a bench harness with record/replay, and today's single-process path measured — 5-trial replay baselines plus a real-model trial |
| [m17-job-store.md](m17-job-store.md) | The durable job store and submission API: a Postgres queue, kill-and-restart testing, and the measured cost of durability with one worker |
| [m18-worker-pool.md](m18-worker-pool.md) | Many workers safely: heartbeats, fencing, bounded retries with a dead letter, an invariant checker that can fail, and an 8-worker kill-and-pause stress test |
| [m19-controlled-execution.md](m19-controlled-execution.md) | Each attempt in a locked-down container: limits, egress grants through a proxy, cancellation, the tests that try to break each limit, and the measured cost |
| [m20-fault-injection.md](m20-fault-injection.md) | Seeded faults at twelve named points in the job protocol plus Postgres restarts, the 26-scenario matrix with the checks after every run, the two bugs it found, and the broken builds it catches |
| [m21-scaling.md](m21-scaling.md) | The task set on 1 to 16 workers on one host: tasks per minute, queue wait and utilization per pool size, where the CPU and the database time went, the bottleneck at each step (the host's CPUs, the batch's end, the cost of a container per job, the provider's rate limit), and the start-up race the measurements found in the bench |
| [m22-records.md](m22-records.md) | Reproducible evaluation records: what every run now records it ran with and produced, the `compare` command that pairs two runs job by job and judges a change with intervals, and one prompt sentence that cut tokens 46% on the real model, with the daily cap that cut the experiment to one round |
| [failure-analysis.md](failure-analysis.md) | Every failure seen with the real agent as the workload, filed as the agent's, the platform's, the harness's, the provider's or the environment's, with how each was found, its evidence and what was done — including the platform bugs fault injection found, healthy jobs dead-lettered under chaos, a test that passed a broken build, the bench timing its own start-up, and every run thrown away |
| [design.md](design.md) | The execution platform's design: why Postgres and `SKIP LOCKED`, the job lifecycle, database-enforced invariants, leases, heartbeats and fencing, the retry policy, what the invariant checker proves and doesn't, cancellation, the execution policy model, what bounds throughput on one host, and what makes two runs comparable |

## Results

Raw per-run results referenced by the write-ups live in [`results/`](results/):

- [`groq-llama-3.3-70b-v1.json`](results/groq-llama-3.3-70b-v1.json) — the M6 baseline (86.7%)
- [`groq-llama-3.3-70b-v2.json`](results/groq-llama-3.3-70b-v2.json) — the M7 hardened run (100%), and the committed baseline the CI gate enforces
- [`bench/`](results/bench/) — the bench trial and summary records: the M16 replay baselines, the real-model trial and superseded runs, the M17 A/B, the M18 regression checks in [`bench/m18/`](results/bench/m18/), the M19 container measurements in [`bench/m19/`](results/bench/m19/), the M20 build comparison in [`bench/m20/`](results/bench/m20/), and the M21 scaling runs in [`bench/m21/`](results/bench/m21/), with the first runs, made before the bench waited for its workers, in [`bench/m21-before-ready/`](results/bench/m21-before-ready/) and the same runs fixed on that first VM in [`bench/m21-previous-vm/`](results/bench/m21-previous-vm/), and the M22 prompt experiment on the real model, its comparison and replays, in [`bench/m22/`](results/bench/m22/)
- [`chaos/`](results/chaos/) — the M20 fault-injection records: the matrix, the hung-transaction runs before their fix, the agent's grading on and off the event loop, and the deliberately broken builds
