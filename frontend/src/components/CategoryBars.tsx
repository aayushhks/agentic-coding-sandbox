import type { ReactNode } from "react";

import { pct } from "../format";

function barTone(rate: number): string {
  if (rate >= 1) return "bg-emerald-500";
  if (rate >= 0.8) return "bg-amber-500";
  return "bg-rose-500";
}

export function CategoryBars({
  byCategory,
  overall,
}: {
  byCategory: Record<string, number>;
  overall: number;
}): ReactNode {
  const rows: [string, number][] = [
    ...Object.entries(byCategory).sort(([a], [b]) => a.localeCompare(b)),
    ["overall", overall],
  ];
  return (
    <div className="flex flex-col gap-2">
      {rows.map(([category, rate]) => (
        <div key={category} className="flex items-center gap-3">
          <span
            className={`w-40 shrink-0 truncate text-sm ${
              category === "overall" ? "font-semibold text-slate-200" : "text-slate-400"
            }`}
          >
            {category}
          </span>
          <div className="h-3 flex-1 overflow-hidden rounded-full bg-slate-800">
            <div
              className={`h-full rounded-full ${barTone(rate)}`}
              style={{ width: `${Math.round(rate * 100)}%` }}
            />
          </div>
          <span className="w-10 shrink-0 text-right text-sm tabular-nums text-slate-300">
            {pct(rate)}
          </span>
        </div>
      ))}
    </div>
  );
}
