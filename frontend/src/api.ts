import type {
  CompareResponse,
  DeploymentReport,
  RunDetail,
  RunSummary,
  TaskDetail,
} from "./types";

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

export async function getDeploymentReport(): Promise<DeploymentReport> {
  try {
    return await getJSON<DeploymentReport>("/deployment-report");
  } catch {
    // Static-export fallback: a backend-free deploy (e.g. S3 + CloudFront) serves the report as a
    // static asset next to the SPA, so the deployment-report view works with no API behind it.
    const response = await fetch("/deployment-report.json");
    if (!response.ok) {
      throw new Error(`deployment report unavailable (${response.status})`);
    }
    return (await response.json()) as DeploymentReport;
  }
}
