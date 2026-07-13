# M14 — Deployment-Report Dashboard

M11–M13 produced the numbers a deployment owner cares about. M14 puts them behind a view a
*non-engineer* stakeholder can read — something to show the customer to prove the deployment works,
not an internal debug tool.

## What it shows

A new **report** tab in the dashboard, backed by the M13 ticket-eval JSON:

- **Headline metrics** — accuracy, resolution rate, correct-escalation rate, false-fix rate,
  injection resistance, mean iterations, and cost + latency per ticket (p50 · p95).
- **Per ticket** — every ticket with its category, expected → actual outcome (green when the agent
  did the right thing, red when it didn't), and the escalation reason when it escalated.
- **Drill-down** — clicking a ticket expands the agent's full step-by-step trace inline, so a
  skeptical reviewer can see exactly what it did — including refusing the prompt-injection ticket.
- The deployment-framed **failure taxonomy**, when non-empty.

## How it's wired

- **Backend**: `GET /api/deployment-report` reads the committed ticket-eval report from a
  configurable path (`deployment_report_path`, default `docs/results/tickets-reference.json`) and
  returns a typed `DeploymentReport`; 404 if the file is absent. Same read-API pattern as the
  benchmark dashboard (M8). The handler is synchronous so the blocking file read runs in FastAPI's
  threadpool, not on the event loop.
- **Frontend**: a typed client (`getDeploymentReport`) and a `DeploymentReport` view built from the
  existing `Stat` / `Panel` primitives. The trace step-list was extracted from `TraceView` into a
  reusable `TraceSteps` component, so the ticket drill-down and the benchmark trace viewer render
  identically. The report tab reads its own endpoint and renders independently of the benchmark
  runs, so it works even when the runs list is empty.

## Honest notes

- **The rendered report is the M13 scripted reference** (an oracle at 100%), so the view has data
  without a live model. Point `deployment_report_path` at a real-model report to show real numbers.
- **Read-only and unauthenticated**, like the rest of the dashboard — sized for a portfolio demo,
  not a multi-tenant service. Baking the report file into the deployed image is an M15 concern.
