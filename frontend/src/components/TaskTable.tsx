import type { ReactNode } from "react";

import { num } from "../format";
import type { TaskResultSummary } from "../types";

export function TaskTable({
  results,
  onSelect,
}: {
  results: TaskResultSummary[];
  onSelect: (taskId: string) => void;
}): ReactNode {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-slate-800 text-left text-xs uppercase tracking-wide text-slate-500">
            <th className="py-2 pr-3 font-medium">task</th>
            <th className="py-2 pr-3 font-medium">category</th>
            <th className="py-2 pr-3 font-medium">diff</th>
            <th className="py-2 pr-3 text-center font-medium">solved</th>
            <th className="py-2 pr-3 text-right font-medium">iters</th>
            <th className="py-2 pr-3 text-right font-medium">tokens</th>
            <th className="py-2 font-medium">failure</th>
          </tr>
        </thead>
        <tbody>
          {results.map((task) => (
            <tr
              key={task.task_id}
              onClick={() => onSelect(task.task_id)}
              className="cursor-pointer border-b border-slate-800/60 hover:bg-slate-800/40"
            >
              <td className="py-2 pr-3 font-mono text-slate-200">{task.task_id}</td>
              <td className="py-2 pr-3 text-slate-400">{task.category}</td>
              <td className="py-2 pr-3 text-slate-400">{task.difficulty}</td>
              <td className="py-2 pr-3 text-center">
                {task.solved ? (
                  <span className="text-emerald-400">✓</span>
                ) : (
                  <span className="text-rose-400">✗</span>
                )}
              </td>
              <td className="py-2 pr-3 text-right tabular-nums text-slate-300">
                {task.iterations}
              </td>
              <td className="py-2 pr-3 text-right tabular-nums text-slate-300">
                {num(task.total_tokens)}
              </td>
              <td className="py-2 font-mono text-xs text-rose-300/80">
                {task.failure_mode ?? ""}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
