import type { ReactNode } from "react";

import { pct } from "../format";

/** A solve-rate pill, green/amber/red by how high the rate is. */
export function SolveBadge({ rate }: { rate: number }): ReactNode {
  const tone =
    rate >= 1
      ? "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30"
      : rate >= 0.8
        ? "bg-amber-500/15 text-amber-300 ring-amber-500/30"
        : "bg-rose-500/15 text-rose-300 ring-rose-500/30";
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ring-1 ${tone}`}>
      {pct(rate)}
    </span>
  );
}

/** A bordered card used to group a section of the dashboard. */
export function Panel({
  title,
  children,
}: {
  title?: string;
  children: ReactNode;
}): ReactNode {
  return (
    <section className="rounded-lg border border-slate-800 bg-slate-900/50 p-4">
      {title && (
        <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-slate-400">
          {title}
        </h3>
      )}
      {children}
    </section>
  );
}

/** A single headline metric: big value over a small label. */
export function Stat({ label, value }: { label: string; value: ReactNode }): ReactNode {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/50 px-4 py-3">
      <div className="text-2xl font-semibold text-slate-100">{value}</div>
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
    </div>
  );
}
