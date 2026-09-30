"""Batch metrics computed from exact job timestamps."""

import math
from collections.abc import Sequence
from typing import Self

from pydantic import BaseModel

from bench.jobs import FailureKind, JobResult, Outcome

PERCENTILE_METHOD = "linear interpolation between closest ranks (numpy's default)"


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile; 0.0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100.0
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


class Distribution(BaseModel):
    n: int
    mean: float
    p50: float
    p95: float
    p99: float
    max: float

    @classmethod
    def of(cls, values: Sequence[float]) -> Self:
        return cls(
            n=len(values),
            mean=sum(values) / len(values) if values else 0.0,
            p50=percentile(values, 50),
            p95=percentile(values, 95),
            p99=percentile(values, 99),
            max=max(values, default=0.0),
        )


class Counts(BaseModel):
    jobs: int
    solved: int
    escalated: int
    failed_task: int
    failed_infra: int
    failed_harness: int
    retries: int
    matched_expectation: int


class TrialMetrics(BaseModel):
    batch_wall_clock_seconds: float
    tasks_per_minute: float
    queue_wait_seconds: Distribution
    service_time_seconds: Distribution
    service_time_excluding_retry_waits_seconds: Distribution
    utilization: float
    utilization_bucket_seconds: float
    utilization_timeline: list[float]
    counts: Counts
    tokens_per_task: Distribution
    prompt_tokens: int
    completion_tokens: int
    llm_calls: int
    retry_wait_seconds: float
    # only filled in against a pinned price list
    cost_usd: float | None = None


def busy_fraction(results: Sequence[JobResult], workers: int, start: float, end: float) -> float:
    """Share of the workers' time spent running jobs between start and end."""
    capacity = workers * (end - start)
    if capacity <= 0:
        return 0.0
    busy = sum(max(0.0, min(r.finished_at, end) - max(r.claimed_at, start)) for r in results)
    return busy / capacity


def utilization_timeline(
    results: Sequence[JobResult], workers: int, start: float, end: float, bucket_seconds: float
) -> list[float]:
    """Busy share of the workers in consecutive buckets from the batch start."""
    buckets = max(1, math.ceil((end - start) / bucket_seconds))
    timeline = []
    for index in range(buckets):
        low = start + index * bucket_seconds
        high = min(low + bucket_seconds, end)
        timeline.append(round(busy_fraction(results, workers, low, high), 4))
    return timeline


def _counts(results: Sequence[JobResult]) -> Counts:
    def failed(kind: FailureKind) -> int:
        return sum(1 for r in results if r.outcome == Outcome.FAILED and r.failure_kind == kind)

    return Counts(
        jobs=len(results),
        solved=sum(1 for r in results if r.outcome == Outcome.SOLVED),
        escalated=sum(1 for r in results if r.outcome == Outcome.ESCALATED),
        failed_task=failed(FailureKind.TASK),
        failed_infra=failed(FailureKind.INFRA),
        failed_harness=failed(FailureKind.HARNESS),
        retries=sum(r.attempts - 1 for r in results),
        matched_expectation=sum(1 for r in results if r.matched_expectation),
    )


def compute_metrics(
    results: Sequence[JobResult], *, workers: int, bucket_seconds: float = 1.0
) -> TrialMetrics:
    start = min((r.submitted_at for r in results), default=0.0)
    end = max((r.finished_at for r in results), default=start)
    wall = end - start
    return TrialMetrics(
        batch_wall_clock_seconds=wall,
        tasks_per_minute=len(results) / (wall / 60.0) if wall > 0 else 0.0,
        queue_wait_seconds=Distribution.of([r.queue_wait for r in results]),
        service_time_seconds=Distribution.of([r.service_time for r in results]),
        service_time_excluding_retry_waits_seconds=Distribution.of(
            [r.service_time - r.retry_wait_seconds for r in results]
        ),
        utilization=busy_fraction(results, workers, start, end),
        utilization_bucket_seconds=bucket_seconds,
        utilization_timeline=utilization_timeline(results, workers, start, end, bucket_seconds),
        counts=_counts(results),
        tokens_per_task=Distribution.of([float(r.total_tokens) for r in results]),
        prompt_tokens=sum(r.prompt_tokens for r in results),
        completion_tokens=sum(r.completion_tokens for r in results),
        llm_calls=sum(r.llm_calls for r in results),
        retry_wait_seconds=sum(r.retry_wait_seconds for r in results),
    )
