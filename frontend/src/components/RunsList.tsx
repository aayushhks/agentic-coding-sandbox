import type { ReactNode } from "react";

import { shortDate } from "../format";
import type { RunSummary } from "../types";
import { SolveBadge } from "./ui";

export function RunsList({
  runs,
  selectedId,
  onSelect,
}: {
  runs: RunSummary[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}): ReactNode {
  return (
    <nav className="flex flex-col gap-2" aria-label="benchmark runs">
      {runs.map((run) => {
        const active = run.id === selectedId;
        return (
          <button
            key={run.id}
            type="button"
            onClick={() => onSelect(run.id)}
            className={`rounded-lg border px-3 py-2 text-left transition ${
              active
                ? "border-sky-500/60 bg-sky-500/10"
                : "border-slate-800 bg-slate-900/40 hover:border-slate-700"
            }`}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="truncate font-medium text-slate-200">{run.label}</span>
              <SolveBadge rate={run.solve_rate} />
            </div>
            <div className="mt-1 flex items-center justify-between text-xs text-slate-500">
              <span className="truncate">{run.model}</span>
              <span className="shrink-0">
                #{run.id} · {shortDate(run.created_at)}
              </span>
            </div>
          </button>
        );
      })}
    </nav>
  );
}
