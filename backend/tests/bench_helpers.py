"""Builders shared by the bench tests."""

from bench.environment import Environment
from bench.jobs import JobResult, Outcome
from bench.metrics import PERCENTILE_METHOD, compute_metrics
from bench.records import BenchConfig, TrialRecord


def make_config(**overrides: object) -> BenchConfig:
    fields: dict[str, object] = {
        "mode": "replay",
        "latency": "zero",
        "executor": "sequential",
        "workers": 1,
        "topology": "single host, one in-process worker",
        "tasks": 2,
        "seed": 1,
        "taskset_version": "v1",
        "taskset_digest": "t" * 64,
        "recordings_digest": "r" * 64,
        "provider": "replay",
        "model": "qwen/qwen3.8-27b",
        "agent_configs": {"benchmark": {"max_iterations": 15}},
        "sandbox_config": {"timeout_seconds": 10.0},
        "tool_transport": "in_process",
        "percentile_method": PERCENTILE_METHOD,
    }
    fields.update(overrides)
    return BenchConfig.model_validate(fields)


def make_environment() -> Environment:
    return Environment(
        git_sha="a" * 40,
        git_dirty=False,
        cpu_model="Test CPU",
        logical_cpus=4,
        usable_cpus=4,
        cpu_quota_cores=None,
        memory_total_gib=16.0,
        memory_limit_gib=None,
        virtualized=True,
        os="Test OS",
        kernel="6.0",
        python="3.13.0",
    )


def make_trial(trial: int, jobs: list[JobResult], **overrides: object) -> TrialRecord:
    fields: dict[str, object] = {
        "label": "demo",
        "trial": trial,
        "started_at": "2026-09-30T00:00:00+00:00",
        "config": make_config(),
        "environment": make_environment(),
        "metrics": compute_metrics(jobs, workers=1),
        "jobs": jobs,
    }
    fields.update(overrides)
    return TrialRecord.model_validate(fields)


def make_job(claimed: float, finished: float, **overrides: object) -> JobResult:
    fields: dict[str, object] = {
        "job_id": f"t{claimed}#0",
        "task_id": f"t{claimed}",
        "repeat": 0,
        "kind": "benchmark",
        "expected": "solve",
        "worker": "w0",
        "attempts": 1,
        "submitted_at": 0.0,
        "claimed_at": claimed,
        "finished_at": finished,
        "outcome": Outcome.SOLVED,
        "failure_mode": None,
        "failure_kind": None,
        "matched_expectation": True,
        "termination_reason": "finished",
        "iterations": 4,
        "llm_calls": 4,
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "retry_wait_seconds": 0.0,
        "divergence": None,
    }
    fields.update(overrides)
    return JobResult.model_validate(fields)
