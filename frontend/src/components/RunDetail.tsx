import { useEffect, useState } from "react";
import type { ReactNode } from "react";

import { getRun } from "../api";
import { num, pct } from "../format";
import type { RunDetail as RunDetailT } from "../types";
import { CategoryBars } from "./CategoryBars";
import { TaskTable } from "./TaskTable";
import { TraceView } from "./TraceView";
import { Panel, Stat } from "./ui";

function FailureTaxonomy({ taxonomy }: { taxonomy: Record<string, number> }): ReactNode {
  const entries = Object.entries(taxonomy);
  if (entries.length === 0) {
    return <p className="text-sm text-emerald-300">no failures — every task solved</p>;
  }
  return (
    <div className="flex flex-wrap gap-2">
      {entries.map(([mode, count]) => (
        <span
          key={mode}
          className="rounded-full bg-rose-500/10 px-3 py-1 text-xs font-medium text-rose-300 ring-1 ring-rose-500/20"
        >
          {mode} · {count}
        </span>
      ))}
    </div>
  );
}

export function RunDetail({ runId }: { runId: number }): ReactNode {
  const [run, setRun] = useState<RunDetailT | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openTask, setOpenTask] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setRun(null);
    setError(null);
    setOpenTask(null);
    getRun(runId)
      .then((data) => active && setRun(data))
      .catch((e: unknown) => active && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      active = false;
    };
  }, [runId]);

  if (error) return <Panel>{<p className="text-sm text-rose-400">{error}</p>}</Panel>;
  if (!run) return <Panel>{<p className="text-sm text-slate-500">loading run…</p>}</Panel>;

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h2 className="text-xl font-semibold text-slate-100">{run.label}</h2>
        <p className="text-sm text-slate-500">
          {run.provider} · {run.model} · benchmark {run.benchmark_version}
        </p>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="solve rate" value={pct(run.solve_rate)} />
        <Stat label="solved" value={`${run.solved_count}/${run.total_tasks}`} />
        <Stat label="avg iterations" value={run.stats.average_iterations.toFixed(1)} />
        <Stat label="total tokens" value={num(run.stats.total_tokens)} />
      </div>

      <Panel title="solve rate by category">
        <CategoryBars byCategory={run.stats.solve_rate_by_category} overall={run.solve_rate} />
      </Panel>

      <Panel title="failure taxonomy">
        <FailureTaxonomy taxonomy={run.stats.failure_taxonomy} />
      </Panel>

      <Panel title="tasks (click a row for the agent trace)">
        <TaskTable results={run.results} onSelect={setOpenTask} />
      </Panel>

      {openTask && (
        <TraceView runId={runId} taskId={openTask} onClose={() => setOpenTask(null)} />
      )}
    </div>
  );
}
