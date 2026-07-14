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
