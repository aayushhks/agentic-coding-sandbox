import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { compareRuns } from "../api";
import { num, pct, signed } from "../format";
import type { CompareResponse, RunSummary, TaskDelta } from "../types";
import { Panel, Stat } from "./ui";

const TRANSITION_TONE: Record<TaskDelta["transition"], string> = {
  converted: "text-emerald-300",
  regressed: "text-rose-300",
  unchanged_pass: "text-slate-400",
  unchanged_fail: "text-slate-500",
};

function LabelSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
}): ReactNode {
  return (
    <label className="flex flex-col gap-1 text-xs uppercase tracking-wide text-slate-500">
      {label}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-md border border-slate-800 bg-slate-900 px-3 py-2 text-sm normal-case text-slate-200"
      >
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    </label>
  );
}

export function CompareView({ runs }: { runs: RunSummary[] }): ReactNode {
  const labels = useMemo(() => [...new Set(runs.map((r) => r.label))], [runs]);
  const [baseline, setBaseline] = useState(labels[labels.length - 1] ?? "");
  const [candidate, setCandidate] = useState(labels[0] ?? "");
  const [comparison, setComparison] = useState<CompareResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!baseline || !candidate) return;
    let active = true;
    setComparison(null);
    setError(null);
    compareRuns(baseline, candidate)
      .then((data) => active && setComparison(data))
      .catch((e: unknown) => active && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      active = false;
    };
  }, [baseline, candidate]);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end gap-4">
        <LabelSelect label="baseline" value={baseline} options={labels} onChange={setBaseline} />
        <span className="pb-2 text-slate-500">→</span>
        <LabelSelect label="candidate" value={candidate} options={labels} onChange={setCandidate} />
      </div>

      {error && <Panel>{<p className="text-sm text-rose-400">{error}</p>}</Panel>}
      {comparison && (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat
              label="solve rate"
              value={`${pct(comparison.baseline_solve_rate)} → ${pct(comparison.candidate_solve_rate)}`}
            />
            <Stat label="solve rate Δ" value={signed(Math.round(comparison.solve_rate_delta * 100))} />
            <Stat label="converted" value={comparison.converted.length} />
            <Stat label="regressed" value={comparison.regressed.length} />
          </div>

          <Panel title="per-task diff">
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="border-b border-slate-800 text-left text-xs uppercase tracking-wide text-slate-500">
                    <th className="py-2 pr-3 font-medium">task</th>
                    <th className="py-2 pr-3 font-medium">transition</th>
                    <th className="py-2 pr-3 text-right font-medium">Δ iters</th>
                    <th className="py-2 text-right font-medium">Δ tokens</th>
                  </tr>
                </thead>
                <tbody>
                  {comparison.deltas.map((delta) => (
                    <tr key={delta.task_id} className="border-b border-slate-800/60">
                      <td className="py-2 pr-3 font-mono text-slate-200">{delta.task_id}</td>
                      <td className={`py-2 pr-3 ${TRANSITION_TONE[delta.transition]}`}>
                        {delta.transition}
                      </td>
                      <td className="py-2 pr-3 text-right tabular-nums text-slate-300">
                        {signed(delta.iteration_delta)}
                      </td>
                      <td className="py-2 text-right tabular-nums text-slate-300">
                        {signed(delta.token_delta)}
                      </td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr className="text-xs text-slate-500">
                    <td className="py-2" colSpan={3}>
                      total token delta
                    </td>
                    <td className="py-2 text-right tabular-nums">
                      {signed(comparison.token_delta)} ({num(comparison.token_delta)})
                    </td>
                  </tr>
                </tfoot>
              </table>
            </div>
          </Panel>
        </>
      )}
    </div>
  );
}
