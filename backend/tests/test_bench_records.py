from pathlib import Path

import pytest

from bench.jobs import FailureKind, Outcome
from bench.records import (
    flatten_scalars,
    load_trials,
    summarize,
    summary_path,
    trial_path,
    write_record,
)
from bench.resources import BatchResources, DatabaseCalls
from tests.bench_helpers import make_config, make_job, make_trial


def test_trial_records_round_trip_through_json(tmp_path: Path) -> None:
    record = make_trial(1, [make_job(0, 1), make_job(1, 3)])
    write_record(record, trial_path(tmp_path, 1))
    assert load_trials(tmp_path) == [record]


def test_load_trials_orders_by_trial_number(tmp_path: Path) -> None:
    for trial in (3, 1, 2):
        write_record(make_trial(trial, [make_job(0, 1)]), trial_path(tmp_path, trial))
    assert [record.trial for record in load_trials(tmp_path)] == [1, 2, 3]


def test_summary_reports_median_and_range_per_metric() -> None:
    trials = [make_trial(n, [make_job(0, wall)]) for n, wall in ((1, 4.0), (2, 2.0), (3, 3.0))]
    summary = summarize("demo", trials)
    wall = summary.metrics["batch_wall_clock_seconds"]
    assert (wall.median, wall.min, wall.max) == (3.0, 2.0, 4.0)
    assert summary.trials == [1, 2, 3]
    assert summary.outcomes_identical


def _resources(busy: float, postgres: float | None) -> BatchResources:
    return BatchResources(
        window_seconds=10.0,
        cpus=4,
        sample_seconds=0.5,
        host_busy_cpu_seconds=busy,
        host_busy_fraction=busy / 40,
        host_steal_fraction=0.0,
        cpu_wait_fraction=None,
        processes={"workers": busy / 2, "postgres": postgres},
        timeline=[(0.5, 0.25, 2)],
    )


def test_a_trial_with_resources_round_trips_and_summarizes_what_every_trial_measured(
    tmp_path: Path,
) -> None:
    calls = {"publish": DatabaseCalls(calls=3, seconds=0.03, max_seconds=0.02)}
    trials = [
        make_trial(1, [make_job(0, 1)], resources=_resources(8.0, 1.0), database=calls),
        # postgres went unmeasured in this one, so its cpu is left out of the summary
        make_trial(2, [make_job(0, 1)], resources=_resources(6.0, None), database=calls),
    ]
    write_record(trials[0], trial_path(tmp_path, 1))
    assert load_trials(tmp_path) == [trials[0]]
    metrics = summarize("demo", trials).metrics
    busy = metrics["resources.host_busy_cpu_seconds"]
    assert (busy.median, busy.min, busy.max) == (7.0, 6.0, 8.0)
    assert metrics["resources.processes.workers"].median == 3.5
    assert "resources.processes.postgres" not in metrics
    # the timeline is a series, not a scalar to summarize
    assert not any(name.startswith("resources.timeline") for name in metrics)
    assert metrics["database.publish.calls"].median == 3


def test_summary_detects_outcomes_that_differ_between_trials() -> None:
    failed = make_job(
        0,
        1,
        outcome=Outcome.FAILED,
        failure_kind=FailureKind.TASK,
        failure_mode="wrong_solution",
        matched_expectation=False,
    )
    summary = summarize("demo", [make_trial(1, [make_job(0, 1)]), make_trial(2, [failed])])
    assert not summary.outcomes_identical


def test_summary_leaves_out_interrupted_trials() -> None:
    trials = [
        make_trial(1, [make_job(0, 1)]),
        make_trial(2, [make_job(0, 9)], interrupted="daily cap reached after 1 of 2 jobs"),
    ]
    summary = summarize("demo", trials)
    assert summary.trials == [1]
    assert summary.interrupted_trials == [2]
    assert summary.metrics["batch_wall_clock_seconds"].max == 1.0


def test_summary_refuses_mixed_configurations() -> None:
    other = make_trial(2, [make_job(0, 1)], config=make_config(seed=2))
    with pytest.raises(ValueError, match="different configurations"):
        summarize("demo", [make_trial(1, [make_job(0, 1)]), other])


def test_summary_needs_a_complete_trial() -> None:
    with pytest.raises(ValueError, match="no complete trials"):
        summarize("demo", [make_trial(1, [make_job(0, 1)], interrupted="stopped")])


def test_flatten_keeps_scalars_and_drops_series() -> None:
    flat = flatten_scalars({"a": 1, "b": {"c": 2.5, "d": [1, 2]}, "e": None, "f": True})
    assert flat == {"a": 1.0, "b.c": 2.5}


def test_summary_is_written_next_to_its_trials(tmp_path: Path) -> None:
    summary = summarize("demo", [make_trial(1, [make_job(0, 1)])])
    path = write_record(summary, summary_path(tmp_path))
    assert path.name == "summary.json"
    assert "queue_wait_seconds.p95" in summary.metrics
    assert "utilization_timeline" not in summary.metrics
