# M9 — CI Eval Gate

A gate that keeps the agent from silently getting worse. M7 built the regression-comparison
primitive (diff two runs, exit non-zero if any task regressed); M8 surfaced it in the
dashboard; M9 turns it into a CI gate against a **committed baseline**, so a drop in the
agent's measured ability fails the build instead of slipping through.

## Why two tiers

A real eval run drives a live model across all 15 tasks — it costs tokens, takes minutes, is
mildly non-deterministic, and needs an API key. That is the wrong thing to run on every push.
So CI is split:

- **Per-push (`ci.yml`)** — the existing backend + frontend unit suites, which now include the
  gate's own logic (`tests/test_eval_gate.py`). This is deterministic, fast, and needs no
  secrets. It proves the gate *works*; it doesn't run a model.
- **Scheduled / on-demand (`eval-gate.yml`)** — runs the real benchmark with Groq and gates the
  result against the committed baseline. Weekly (`cron`) plus `workflow_dispatch`. It needs the
  `GROQ_API_KEY` secret and skips cleanly (stays green) when that secret is absent, so forks and
  unconfigured schedules don't fail noisily.

## The gate rule

Given a candidate run and a committed baseline, the gate (`app/eval/gate.py`) **fails** if
either holds:

1. **Any task regressed** — passed in the baseline, fails in the candidate. This is the strict,
   per-task guarantee; a single task slipping from pass to fail trips it.
2. **Solve rate below a floor** — the candidate's overall solve rate is under `--min-solve-rate`
   (the workflow uses `1.0`). An absolute backstop independent of the baseline.

The baseline is a version-controlled results JSON
([`docs/results/groq-llama-3.3-70b-v2.json`](results/groq-llama-3.3-70b-v2.json), the clean
100% M7 run). Because it lives in the repo, the expected behavior is reviewed and changed
deliberately, in a commit — the contract is explicit.

## Components

- `app/eval/gate.py` — `load_baseline_rows()` parses a committed results JSON into comparison
  rows; `evaluate_gate(comparison, min_solve_rate)` returns a `GateOutcome` (passed + reasons),
  reusing M7's `compare_runs`.
- `app/eval/gate_cli.py` — loads the latest candidate run by label from the database, diffs it
  against the baseline JSON, applies the rule, prints a summary, and exits `1` on failure.
- `.github/workflows/eval-gate.yml` — runs `app.eval.cli` (candidate) then `app.eval.gate_cli`
  (gate) with the real model.

## Running it locally

```bash
cd backend
# produce a candidate run (real model)
LLM_PROVIDER=groq DATABASE_URL="sqlite+aiosqlite:///eval.db" \
  uv run python -m app.eval.cli --label ci-gate --create-tables --require-verified-finish

# gate it against the committed baseline
DATABASE_URL="sqlite+aiosqlite:///eval.db" uv run python -m app.eval.gate_cli \
  --baseline ../docs/results/groq-llama-3.3-70b-v2.json --candidate ci-gate --min-solve-rate 1.0
```

Gating the committed runs already in the database shows both verdicts — the v2 run passes, the
v1 run (which predates the M7 fixes) fails on exactly the two tasks M7 repaired:

```
$ ... gate_cli --candidate groq-llama-3.3-70b-v2-hardened --min-solve-rate 1.0
solve rate: 100.0% -> 100.0%  (floor 100.0%)
GATE PASSED

$ ... gate_cli --candidate groq-llama-3.3-70b --min-solve-rate 1.0
solve rate: 100.0% -> 86.7%  (floor 100.0%)
regressed: ['fix_binary_search', 'lru_cache']
GATE FAILED:
  - 2 task(s) regressed: fix_binary_search, lru_cache
  - solve rate 86.7% is below the floor 100.0%
```

## Honest notes

- **Non-determinism is real.** A single live run at `temperature=0` is not bit-for-bit
  reproducible, so a strict per-task gate can flag a flaky task as a regression. The mitigations
  here are pragmatic, not bulletproof: the gate runs off-the-critical-path (not per-push) and
  pairs the regression check with an absolute floor. A sturdier version would run best-of-N or
  require a regression to repeat before failing — deferred as out of scope for this milestone.
- **The baseline is a single run.** It is a committed reference point, not a statistical
  distribution. Updating it is a deliberate, reviewable commit.
- **Skips without a secret.** No `GROQ_API_KEY` means the scheduled gate no-ops green rather
  than failing; the per-push unit gate still runs everywhere.
