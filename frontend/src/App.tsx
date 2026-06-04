import { useEffect, useState } from "react";
import type { ReactNode } from "react";

import { listRuns } from "./api";
import { CompareView } from "./components/CompareView";
import { RunDetail } from "./components/RunDetail";
import { RunsList } from "./components/RunsList";
import type { RunSummary } from "./types";

type Tab = "runs" | "compare";

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}): ReactNode {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-md px-3 py-1.5 text-sm font-medium transition ${
        active ? "bg-sky-500/15 text-sky-300" : "text-slate-400 hover:text-slate-200"
      }`}
    >
      {children}
    </button>
  );
}

export default function App(): ReactNode {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [tab, setTab] = useState<Tab>("runs");

  useEffect(() => {
    listRuns()
      .then((data) => {
        setRuns(data);
        setSelectedId((current) => current ?? data[0]?.id ?? null);
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  return (
    <div className="mx-auto flex min-h-screen max-w-6xl flex-col px-4 py-6">
      <header className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-100">eval dashboard</h1>
          <p className="text-sm text-slate-500">
            agentic coding sandbox — benchmark runs, traces, and regression diffs
          </p>
        </div>
        <nav className="flex gap-1 rounded-lg border border-slate-800 bg-slate-900/50 p-1">
          <TabButton active={tab === "runs"} onClick={() => setTab("runs")}>
            runs
          </TabButton>
          <TabButton active={tab === "compare"} onClick={() => setTab("compare")}>
            compare
          </TabButton>
        </nav>
      </header>

      <main className="flex-1">
        {error && (
          <p className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-300">
            could not load runs: {error}. is the backend running?
          </p>
        )}

        {!error && runs && runs.length === 0 && (
          <p className="text-sm text-slate-500">
            no benchmark runs yet — run <code className="text-slate-300">app.eval.cli</code> to
            produce one.
          </p>
        )}

        {!error && runs && runs.length > 0 && tab === "runs" && (
          <div className="grid gap-6 md:grid-cols-[18rem_1fr]">
            <RunsList runs={runs} selectedId={selectedId} onSelect={setSelectedId} />
            {selectedId !== null && <RunDetail runId={selectedId} />}
          </div>
        )}

        {!error && runs && runs.length > 0 && tab === "compare" && <CompareView runs={runs} />}
      </main>
    </div>
  );
}
