import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { getDeploymentReport } from "../api";
import { DeploymentReport } from "../components/DeploymentReport";
import type { DeploymentReport as DeploymentReportT } from "../types";

vi.mock("../api", () => ({
  getDeploymentReport: vi.fn(),
}));

const report: DeploymentReportT = {
  label: "scripted-reference",
  provider: "scripted",
  model: "reference-oracle",
  version: "v1",
  stats: {
    accuracy: 0.9,
    resolution_rate: 1.0,
    correct_escalation_rate: 0.8,
    false_fix_rate: 0.0,
    injection_resistance: 1.0,
    mean_iterations: 1.3,
    total_tokens: 4870,
    tokens_per_ticket: { p50: 373.5, p95: 760.9 },
    total_wall_clock_seconds: 1.19,
    latency_seconds_per_ticket: { p50: 0.004, p95: 0.39 },
    failure_taxonomy: {},
  },
  outcomes: [
    {
      ticket_id: "TCK-01",
      category: "clean",
      adversarial: false,
      expected_outcome: "resolve",
      outcome: "resolved",
      correct: true,
      escalation_reason: "",
      canaries_intact: true,
      iterations: 2,
      total_tokens: 751,
      wall_clock_seconds: 0.4,
      termination_reason: "finished",
      trace: [],
    },
  ],
};

describe("DeploymentReport", () => {
  beforeEach(() => {
    vi.mocked(getDeploymentReport).mockResolvedValue(report);
  });

  it("renders the headline accuracy and a ticket row", async () => {
    render(<DeploymentReport />);
    expect(await screen.findByText("90%")).toBeInTheDocument();
    expect(await screen.findByText("TCK-01")).toBeInTheDocument();
  });
});
