import pytest

from bench.jobs import FailureKind, Outcome
from bench.metrics import Distribution, compute_metrics, percentile
from tests.bench_helpers import make_job as _job


def test_percentile_interpolates_between_closest_ranks() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 50) == 30.0
    assert percentile(values, 95) == pytest.approx(48.0)
    assert percentile(values, 99) == pytest.approx(49.6)
    assert percentile([7.0], 95) == 7.0
    assert percentile([], 50) == 0.0


def test_distribution_summarizes_a_sample() -> None:
    dist = Distribution.of([1.0, 2.0, 3.0, 4.0])
    assert (dist.n, dist.mean, dist.p50, dist.max) == (4, 2.5, 2.5, 4.0)


def test_back_to_back_jobs_keep_one_worker_fully_busy() -> None:
    metrics = compute_metrics([_job(0, 1), _job(1, 3), _job(3, 4)], workers=1)
    assert metrics.batch_wall_clock_seconds == 4.0
    assert metrics.tasks_per_minute == pytest.approx(45.0)
    assert metrics.queue_wait_seconds.max == 3.0
    assert metrics.queue_wait_seconds.p50 == 1.0
    assert metrics.service_time_seconds.p50 == 1.0
    assert metrics.utilization == 1.0
    assert metrics.utilization_timeline == [1.0, 1.0, 1.0, 1.0]


def test_idle_gaps_lower_utilization() -> None:
    metrics = compute_metrics([_job(0, 1), _job(2, 3)], workers=1)
    assert metrics.utilization == pytest.approx(2 / 3)
    assert metrics.utilization_timeline == [1.0, 0.0, 1.0]


def test_utilization_is_shared_across_workers() -> None:
    metrics = compute_metrics([_job(0, 2, worker="w0"), _job(0, 1, worker="w1")], workers=2)
    assert metrics.utilization == 0.75
    assert metrics.utilization_timeline == [1.0, 0.5]


def test_last_bucket_can_be_partial() -> None:
    metrics = compute_metrics([_job(0, 2.5)], workers=1)
    assert metrics.utilization_timeline == [1.0, 1.0, 1.0]


def test_retry_waits_are_separated_from_service_time() -> None:
    metrics = compute_metrics([_job(0, 10, retry_wait_seconds=4.0)], workers=1)
    assert metrics.service_time_seconds.p50 == 10.0
    assert metrics.service_time_excluding_retry_waits_seconds.p50 == 6.0
    assert metrics.retry_wait_seconds == 4.0


def test_counts_split_outcomes_and_failure_kinds() -> None:
    failed = {"outcome": Outcome.FAILED, "matched_expectation": False}
    results = [
        _job(0, 1),
        _job(1, 2, outcome=Outcome.ESCALATED),
        _job(2, 3, **failed, failure_kind=FailureKind.TASK, failure_mode="wrong_solution"),
        _job(3, 4, **failed, failure_kind=FailureKind.INFRA, failure_mode="provider_error"),
        _job(4, 5, **failed, failure_kind=FailureKind.HARNESS, attempts=3),
    ]
    counts = compute_metrics(results, workers=1).counts
    assert (counts.jobs, counts.solved, counts.escalated) == (5, 1, 1)
    assert (counts.failed_task, counts.failed_infra, counts.failed_harness) == (1, 1, 1)
    assert counts.retries == 2
    assert counts.matched_expectation == 2


def test_token_totals_and_calls_are_summed() -> None:
    metrics = compute_metrics([_job(0, 1), _job(1, 2, llm_calls=6)], workers=1)
    assert (metrics.prompt_tokens, metrics.completion_tokens, metrics.llm_calls) == (200, 40, 10)
    assert metrics.tokens_per_task.p50 == 120.0
    assert metrics.cost_usd is None


def test_an_empty_batch_has_zeroed_metrics() -> None:
    metrics = compute_metrics([], workers=1)
    assert metrics.batch_wall_clock_seconds == 0.0
    assert metrics.tasks_per_minute == 0.0
    assert metrics.utilization == 0.0
