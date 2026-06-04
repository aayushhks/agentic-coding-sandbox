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

## Results

Raw per-run results referenced by the write-ups live in [`results/`](results/):

- [`groq-llama-3.3-70b-v1.json`](results/groq-llama-3.3-70b-v1.json) — the M6 baseline (86.7%)
- [`groq-llama-3.3-70b-v2.json`](results/groq-llama-3.3-70b-v2.json) — the M7 hardened run (100%), and the committed baseline the CI gate enforces
