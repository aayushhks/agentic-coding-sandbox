import { afterEach, describe, expect, it, vi } from "vitest";

import { getDeploymentReport } from "../api";

describe("getDeploymentReport", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("falls back to the static report when the API is unavailable", async () => {
    const payload = {
      label: "static",
      provider: "scripted",
      model: "reference-oracle",
      version: "v1",
      stats: {},
      outcomes: [],
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, status: 404, statusText: "Not Found" })
      .mockResolvedValueOnce({ ok: true, json: async () => payload });
    vi.stubGlobal("fetch", fetchMock);

    const report = await getDeploymentReport();

    expect(report.label).toBe("static");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[1][0])).toContain("/deployment-report.json");
  });
});

describe("static data mode", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  it("reads every call from the committed snapshots instead of the api", async () => {
    vi.stubEnv("VITE_STATIC_DATA", "true");
    vi.resetModules();
    const api = await import("../api");
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);

    await api.listRuns();
    await api.getRun(2);
    await api.getTask(2, "two_sum");
    await api.compareRuns("base", "cand");
    await api.getDeploymentReport();

    expect(fetchMock.mock.calls.map((call) => String(call[0]))).toEqual([
      "/static-api/runs.json",
      "/static-api/runs/2.json",
      "/static-api/runs/2/tasks/two_sum.json",
      "/static-api/compare/base__cand.json",
      "/deployment-report.json",
    ]);
  });
});
