import type { CompareResponse, RunDetail, RunSummary, TaskDetail } from "./types";

// Same-origin by default (dev proxy / single-process deploy). When the dashboard is hosted
// apart from the API (e.g. Vercel -> Railway), set VITE_API_BASE_URL to the API's origin.
const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";

async function getJSON<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}/api${path}`);
  if (!response.ok) {
    throw new Error(`request to ${path} failed: ${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

export function listRuns(): Promise<RunSummary[]> {
  return getJSON<RunSummary[]>("/runs");
}

export function getRun(runId: number): Promise<RunDetail> {
  return getJSON<RunDetail>(`/runs/${runId}`);
}

export function getTask(runId: number, taskId: string): Promise<TaskDetail> {
  return getJSON<TaskDetail>(`/runs/${runId}/tasks/${encodeURIComponent(taskId)}`);
}

export function compareRuns(baseline: string, candidate: string): Promise<CompareResponse> {
  const query = new URLSearchParams({ baseline, candidate });
  return getJSON<CompareResponse>(`/compare?${query.toString()}`);
}
