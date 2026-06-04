import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RunsList } from "../components/RunsList";
import type { RunSummary } from "../types";

function run(id: number, label: string, solveRate: number): RunSummary {
  return {
    id,
    created_at: "2026-06-04T00:00:00",
    label,
    provider: "groq",
    model: "llama-3.3-70b-versatile",
    benchmark_version: "v1",
    total_tasks: 15,
    solved_count: Math.round(solveRate * 15),
    solve_rate: solveRate,
    stats: {
      solve_rate: solveRate,
      average_iterations: 5,
      total_tokens: 1000,
      solve_rate_by_category: {},
      failure_taxonomy: {},
    },
  };
}

const runs = [run(2, "v2-hardened", 1), run(1, "v1-baseline", 0.8667)];

describe("RunsList", () => {
  it("renders each run with its label and solve rate", () => {
    render(<RunsList runs={runs} selectedId={2} onSelect={() => {}} />);
    expect(screen.getByText("v2-hardened")).toBeInTheDocument();
    expect(screen.getByText("v1-baseline")).toBeInTheDocument();
    expect(screen.getByText("100%")).toBeInTheDocument();
    expect(screen.getByText("87%")).toBeInTheDocument();
  });

  it("calls onSelect with the run id when a run is clicked", async () => {
    const onSelect = vi.fn();
    render(<RunsList runs={runs} selectedId={2} onSelect={onSelect} />);
    await userEvent.click(screen.getByText("v1-baseline"));
    expect(onSelect).toHaveBeenCalledWith(1);
  });
});
