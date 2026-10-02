import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import committed from "../../public/platform-report.json";
import { getPlatformReport } from "../api";
import { PlatformReport } from "../components/PlatformReport";
import type { PlatformReport as PlatformReportT } from "../types";

vi.mock("../api", () => ({
  getPlatformReport: vi.fn(),
}));

const report = committed as PlatformReportT;

describe("PlatformReport", () => {
  beforeEach(() => {
    vi.mocked(getPlatformReport).mockReset();
  });

  it("renders every claim of the committed report with its configuration and records", async () => {
    vi.mocked(getPlatformReport).mockResolvedValue(report);
    render(<PlatformReport />);
    for (const claim of report.headline) {
      expect(await screen.findByText(claim.value)).toBeInTheDocument();
      expect(screen.getAllByText(claim.config, { exact: false }).length).toBeGreaterThan(0);
    }
    const links = screen.getAllByRole("link").map((link) => link.getAttribute("href"));
    for (const path of report.headline.flatMap((claim) => claim.sources)) {
      expect(links).toContain(`${report.repository}/blob/main/${path}`);
    }
  });

  it("renders every table cell straight from the file, unchanged", async () => {
    vi.mocked(getPlatformReport).mockResolvedValue(report);
    render(<PlatformReport />);
    await screen.findByText(report.headline[0].value);
    for (const section of report.sections) {
      for (const table of section.tables) {
        expect(screen.getByText(table.title)).toBeInTheDocument();
        for (const cell of table.rows[0]) {
          expect(screen.getAllByText(cell).length).toBeGreaterThan(0);
        }
      }
    }
  });

  it("says so when the report can't be loaded", async () => {
    vi.mocked(getPlatformReport).mockRejectedValue(new Error("404"));
    render(<PlatformReport />);
    expect(await screen.findByText(/could not load the platform report: 404/)).toBeInTheDocument();
  });
});
