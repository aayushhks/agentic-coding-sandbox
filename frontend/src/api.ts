import type {
  CompareResponse,
  DeploymentReport,
  RunDetail,
  RunSummary,
  TaskDetail,
} from "./types";

// Same-origin by default (dev proxy / single-process deploy). When the dashboard is hosted
// apart from the API, set VITE_API_BASE_URL to the API's origin.
const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";
// A static host (e.g. Vercel) has no API behind it: with VITE_STATIC_DATA set, every read comes
// from the committed snapshots of the API under /static-api/ (backend app/api/static_export.py).
const STATIC_DATA = import.meta.env.VITE_STATIC_DATA === "true";

async function fetchJSON<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`request to ${url} failed: ${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

// `snapshot` is the file under /static-api/ holding this call's committed answer
function getJSON<T>(path: string, snapshot: string): Promise<T> {
  return fetchJSON<T>(STATIC_DATA ? `/static-api/${snapshot}` : `${API_BASE}/api${path}`);
}

export function listRuns(): Promise<RunSummary[]> {
  return getJSON<RunSummary[]>("/runs", "runs.json");
}

export function getRun(runId: number): Promise<RunDetail> {
  return getJSON<RunDetail>(`/runs/${runId}`, `runs/${runId}.json`);
}

export function getTask(runId: number, taskId: string): Promise<TaskDetail> {
  const task = encodeURIComponent(taskId);
  return getJSON<TaskDetail>(`/runs/${runId}/tasks/${task}`, `runs/${runId}/tasks/${task}.json`);
}

export function compareRuns(baseline: string, candidate: string): Promise<CompareResponse> {
  const query = new URLSearchParams({ baseline, candidate });
  const snapshot = `compare/${encodeURIComponent(baseline)}__${encodeURIComponent(candidate)}.json`;
  return getJSON<CompareResponse>(`/compare?${query.toString()}`, snapshot);
}

export async function getDeploymentReport(): Promise<DeploymentReport> {
  if (!STATIC_DATA) {
    try {
      return await fetchJSON<DeploymentReport>(`${API_BASE}/api/deployment-report`);
    } catch {
      // a backend-free deploy serves the report as a static asset next to the SPA instead
    }
  }
  return fetchJSON<DeploymentReport>("/deployment-report.json");
}
