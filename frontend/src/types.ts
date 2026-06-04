// Mirrors the Pydantic response models in backend/app/api/schemas.py.

export interface RunStats {
  solve_rate: number;
  average_iterations: number;
  total_tokens: number;
  solve_rate_by_category: Record<string, number>;
  failure_taxonomy: Record<string, number>;
}

export interface RunSummary {
  id: number;
  created_at: string;
  label: string;
  provider: string;
  model: string;
  benchmark_version: string;
  total_tasks: number;
  solved_count: number;
  solve_rate: number;
  stats: RunStats;
}

export interface TaskResultSummary {
  task_id: string;
  category: string;
  difficulty: string;
  solved: boolean;
  iterations: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  wall_clock_seconds: number;
  failure_mode: string | null;
  tool_counts: Record<string, number>;
}

export interface RunDetail extends RunSummary {
  results: TaskResultSummary[];
}

export interface TraceStep {
  index: number;
  thought?: string | null;
  raw_response?: string | null;
  tool?: string | null;
  arguments?: Record<string, unknown> | null;
  observation?: string | null;
  malformed?: boolean;
  prompt_tokens?: number;
  completion_tokens?: number;
  tool_ok?: boolean | null;
  exit_code?: number | null;
  timed_out?: boolean | null;
}

export interface TaskDetail extends TaskResultSummary {
  run_id: number;
  trace: TraceStep[];
  eval_exit_code: number | null;
  eval_output: string;
}

export interface TaskDelta {
  task_id: string;
  category: string;
  difficulty: string;
  transition: "converted" | "regressed" | "unchanged_pass" | "unchanged_fail";
  baseline_solved: boolean;
  candidate_solved: boolean;
  baseline_failure: string | null;
  candidate_failure: string | null;
  iteration_delta: number;
  token_delta: number;
}

export interface CompareResponse {
  baseline_label: string;
  candidate_label: string;
  baseline_solve_rate: number;
  candidate_solve_rate: number;
  solve_rate_delta: number;
  token_delta: number;
  converted: string[];
  regressed: string[];
  deltas: TaskDelta[];
}
