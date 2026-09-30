"""Builders shared by the bench tests."""

from bench.jobs import JobResult, Outcome


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
