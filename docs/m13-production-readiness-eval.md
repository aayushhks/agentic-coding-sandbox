# M13 — Production-Readiness Eval

M5–M7 measured whether the agent solved a benchmark task. A deployment owner asks a different
question: on real tickets, how often does it resolve the right ones, escalate the ones it should,
avoid confidently breaking things, and resist a hijack attempt — and what does a resolution cost?
M13 reframes the eval around those questions, over the M12 messy-ticket dataset.

## The metrics

`app/tickets/eval.py` runs `resolve_ticket` across the dataset (timing each) and produces a
`TicketEvalReport`:

- **resolution rate** — of the resolvable tickets, how many were actually fixed (hidden tests pass).
- **correct-escalation rate** — of the tickets that should be escalated, how many were.
- **false-fix rate** — how often the agent finished claiming a fix that isn't one.
- **injection resistance** — of the adversarial tickets, how many it refused (escalated with the
  canary intact) — the M12 canary check, promoted to a headline number.
- **accuracy** — overall, did it do the right thing.
- **mean iterations**, and a deployment-framed **failure taxonomy** (`false_fix`,
  `missed_escalation`, `over_escalation`, `unresolved`, `unsafe_action`).

## Cost and latency

"What does a resolution cost" is the first operational question, so the report carries **tokens and
wall-clock time per ticket**, reported as totals and as **p50 / p95** — the tail matters more than
the mean when you pay per ticket.

## Running it, and comparing runs

```bash
# real model (needs GROQ_API_KEY); writes a JSON report
LLM_PROVIDER=groq python -m app.tickets.eval_cli --label groq-tickets --output docs/results/tickets.json

# diff two reports; exits non-zero if a ticket the agent handled correctly now regresses
python -m app.tickets.compare_cli --baseline base.json --candidate cand.json
```

Reports are JSON — the same static export the deployment demo serves to the dashboard, and the
substrate for the M7-style regression diff, now framed as "a ticket that was handled correctly is
now handled wrong."

## The reference report

[`results/tickets-reference.json`](results/tickets-reference.json) is a **scripted-reference** run:
each ticket is driven by a scripted agent that does the right thing, so the harness is exercised
end-to-end over the real dataset and the dashboard has data to render. It is **not** a real-model
measurement — it shows what a perfect run looks like (100% across the board). The real numbers come
from running the CLI with a live model, exactly as M6 did for the benchmark.

## Honest notes

- **The reference report is an oracle, not a model.** It exercises the harness, not the agent's
  ability; a real-model run is what characterizes the agent.
- **Small dataset.** Ten tickets characterize behavior and cost, not a statistical guarantee.
- **Injection resistance is over one adversarial ticket** here; the metric generalizes, but a
  single case is a probe, not a distribution.
