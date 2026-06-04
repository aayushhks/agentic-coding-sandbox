import type { CompareResponse, RunDetail, RunSummary, TaskDetail } from "./types";

async function getJSON<T>(path: string): Promise<T> {
  const response = await fetch(`/api${path}`);
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
