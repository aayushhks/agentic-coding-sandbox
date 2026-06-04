import { useEffect, useState } from "react";
import type { ReactNode } from "react";

import { getTask } from "../api";
import type { TaskDetail, TraceStep } from "../types";

function StepCard({ step }: { step: TraceStep }): ReactNode {
  return (
    <li className="rounded-md border border-slate-800 bg-slate-900/60 p-3">
      <div className="mb-1 flex items-center gap-2 text-xs">
        <span className="font-mono text-slate-500">#{step.index}</span>
        {step.malformed ? (
          <span className="rounded bg-rose-500/15 px-1.5 py-0.5 font-semibold text-rose-300">
            malformed
          </span>
        ) : (
          <span className="rounded bg-sky-500/15 px-1.5 py-0.5 font-mono text-sky-300">
            {step.tool ?? "—"}
          </span>
        )}
        {step.tool_ok === false && <span className="text-rose-400">exit {step.exit_code}</span>}
        {step.timed_out && <span className="text-amber-400">timed out</span>}
      </div>
      {step.thought && <p className="mb-1 text-sm text-slate-300">{step.thought}</p>}
      {step.arguments && Object.keys(step.arguments).length > 0 && (
        <pre className="mb-1 overflow-x-auto rounded bg-slate-950/70 p-2 text-xs text-slate-400">
          {JSON.stringify(step.arguments, null, 2)}
        </pre>
      )}
      {step.observation && (
        <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-slate-950/70 p-2 text-xs text-slate-400">
          {step.observation}
        </pre>
      )}
    </li>
  );
}

export function TraceView({
  runId,
  taskId,
  onClose,
}: {
  runId: number;
  taskId: string;
  onClose: () => void;
}): ReactNode {
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setTask(null);
    setError(null);
    getTask(runId, taskId)
      .then((data) => active && setTask(data))
      .catch((e: unknown) => active && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      active = false;
    };
  }, [runId, taskId]);

  return (
    <div
      className="fixed inset-0 z-10 flex justify-end bg-black/60"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="flex h-full w-full max-w-2xl flex-col border-l border-slate-800 bg-slate-950 shadow-xl"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label={`trace for ${taskId}`}
      >
        <header className="flex items-center justify-between border-b border-slate-800 px-5 py-3">
          <div>
            <h2 className="font-mono text-lg text-slate-100">{taskId}</h2>
            {task && (
              <p className="text-xs text-slate-500">
                {task.solved ? "solved" : `failed · ${task.failure_mode}`} · {task.iterations}{" "}
                iterations · eval exit {task.eval_exit_code}
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-slate-200"
            aria-label="close trace"
          >
            ✕
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          {error && <p className="text-sm text-rose-400">{error}</p>}
          {!task && !error && <p className="text-sm text-slate-500">loading trace…</p>}
          {task && (
            <>
              <ol className="flex flex-col gap-2">
                {task.trace.map((step, i) => (
                  <StepCard key={i} step={step} />
                ))}
              </ol>
              {task.eval_output && (
                <div className="mt-4">
                  <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
                    evaluation output
                  </h3>
                  <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-slate-900 p-3 text-xs text-slate-400">
                    {task.eval_output}
                  </pre>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
