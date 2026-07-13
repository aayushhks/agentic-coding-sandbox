import { Fragment, useEffect, useState } from "react";
import type { ReactNode } from "react";

import { getDeploymentReport } from "../api";
import { num, pct } from "../format";
import type { DeploymentReport as DeploymentReportT, DeploymentStats } from "../types";
import { TraceSteps } from "./TraceView";
import { Panel, Stat } from "./ui";

/** Tokens rounded to whole numbers, e.g. {p50: 373.5, p95: 760.9} -> "374 · 761". */
function tokenSpread(spread: { p50: number; p95: number }): string {
  return `${num(Math.round(spread.p50))} · ${num(Math.round(spread.p95))}`;
}

/** Seconds to two decimals, e.g. {p50: 0.004, p95: 0.39} -> "0.00s · 0.39s". */
function latencySpread(spread: { p50: number; p95: number }): string {
  return `${spread.p50.toFixed(2)}s · ${spread.p95.toFixed(2)}s`;
}

function Headline({ stats }: { stats: DeploymentStats }): ReactNode {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <Stat label="accuracy" value={pct(stats.accuracy)} />
      <Stat label="resolution rate" value={pct(stats.resolution_rate)} />
      <Stat label="correct-escalation rate" value={pct(stats.correct_escalation_rate)} />
      <Stat label="false-fix rate" value={pct(stats.false_fix_rate)} />
      <Stat label="injection resistance" value={pct(stats.injection_resistance)} />
      <Stat label="mean iterations" value={stats.mean_iterations.toFixed(1)} />
      <Stat label="tokens/ticket p50·p95" value={tokenSpread(stats.tokens_per_ticket)} />
      <Stat label="latency/ticket p50·p95" value={latencySpread(stats.latency_seconds_per_ticket)} />
    </div>
  );
}

function FailureTaxonomy({ taxonomy }: { taxonomy: Record<string, number> }): ReactNode {
  return (
    <Panel title="failure taxonomy">
      <div className="flex flex-wrap gap-2">
        {Object.entries(taxonomy).map(([mode, count]) => (
          <span
            key={mode}
            className="rounded-full bg-rose-500/10 px-3 py-1 text-xs font-medium text-rose-300 ring-1 ring-rose-500/20"
          >
            {mode} · {count}
          </span>
        ))}
      </div>
    </Panel>
  );
}

export function DeploymentReport(): ReactNode {
  const [report, setReport] = useState<DeploymentReportT | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openTicket, setOpenTicket] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    getDeploymentReport()
      .then((data) => active && setReport(data))
      .catch((e: unknown) => active && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      active = false;
    };
  }, []);

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h2 className="text-xl font-semibold text-slate-100">deployment report</h2>
        <p className="text-sm text-slate-500">
          how the agent handled a batch of real support tickets — which it resolved, which it
          escalated to a human instead of guessing, and what each resolution cost.
        </p>
      </div>

      {error && <Panel>{<p className="text-sm text-rose-400">{error}</p>}</Panel>}
      {!report && !error && (
        <Panel>{<p className="text-sm text-slate-500">loading deployment report…</p>}</Panel>
      )}

      {report && (
        <>
          <p className="text-sm text-slate-500">
            {report.label} · {report.provider} · {report.model} · {report.outcomes.length} tickets
          </p>

          <Headline stats={report.stats} />

          <Panel title="tickets (click a row for the agent's step-by-step trace)">
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="border-b border-slate-800 text-left text-xs uppercase tracking-wide text-slate-500">
                    <th className="py-2 pr-3 font-medium">ticket</th>
                    <th className="py-2 pr-3 font-medium">category</th>
                    <th className="py-2 pr-3 font-medium">expected → outcome</th>
                    <th className="py-2 font-medium">escalation reason</th>
                  </tr>
                </thead>
                <tbody>
                  {report.outcomes.map((outcome) => {
                    const open = openTicket === outcome.ticket_id;
                    return (
                      <Fragment key={outcome.ticket_id}>
                        <tr
                          onClick={() => setOpenTicket(open ? null : outcome.ticket_id)}
                          className="cursor-pointer border-b border-slate-800/60 hover:bg-slate-800/40"
                        >
                          <td className="py-2 pr-3 font-mono text-slate-200">
                            {outcome.ticket_id}
                          </td>
                          <td className="py-2 pr-3 text-slate-400">{outcome.category}</td>
                          <td
                            className={`py-2 pr-3 ${
                              outcome.correct ? "text-emerald-300" : "text-rose-300"
                            }`}
                          >
                            {outcome.expected_outcome} → {outcome.outcome}
                          </td>
                          <td className="py-2 text-slate-400">{outcome.escalation_reason}</td>
                        </tr>
                        {open && (
                          <tr className="border-b border-slate-800/60 bg-slate-950/40">
                            <td colSpan={4} className="p-3">
                              <TraceSteps steps={outcome.trace} />
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Panel>

          {Object.keys(report.stats.failure_taxonomy).length > 0 && (
            <FailureTaxonomy taxonomy={report.stats.failure_taxonomy} />
          )}
        </>
      )}
    </div>
  );
}
