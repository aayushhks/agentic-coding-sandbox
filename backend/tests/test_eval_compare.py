import pytest

from app.eval.compare import TaskRow, Transition, compare_runs


def _row(
    task_id: str,
    solved: bool,
    *,
    failure: str | None = None,
    iterations: int = 4,
    tokens: int = 1000,
    category: str = "algorithms",
    difficulty: str = "easy",
) -> TaskRow:
    return TaskRow(task_id, category, difficulty, solved, failure, iterations, tokens)


def test_detects_conversion_and_regression() -> None:
    baseline = [
        _row("a", False, failure="wrong_solution"),
        _row("b", True),
        _row("c", True),
        _row("d", False, failure="timed_out"),
    ]
    candidate = [
        _row("a", True, iterations=6, tokens=1500),
        _row("b", False, failure="exhausted_iterations"),
        _row("c", True),
        _row("d", False, failure="timed_out"),
    ]
    comparison = compare_runs("v1", "v2", baseline, candidate)

    assert {d.task_id for d in comparison.converted} == {"a"}
    assert {d.task_id for d in comparison.regressed} == {"b"}
    assert comparison.has_regression

    converted = next(d for d in comparison.deltas if d.task_id == "a")
    assert converted.transition == Transition.CONVERTED
    assert converted.iteration_delta == 2
    assert converted.token_delta == 500

    assert comparison.baseline_solved_count == 2
    assert comparison.candidate_solved_count == 2
    assert comparison.solve_rate_delta == 0.0


def test_no_regression_when_only_conversions() -> None:
    baseline = [_row("a", False), _row("b", True)]
    candidate = [_row("a", True), _row("b", True)]
    comparison = compare_runs("v1", "v2", baseline, candidate)

    assert not comparison.has_regression
    assert comparison.candidate_solve_rate == 1.0
    assert comparison.solve_rate_delta == 0.5


def test_mismatched_task_sets_raise() -> None:
    with pytest.raises(ValueError, match="different tasks"):
        compare_runs("v1", "v2", [_row("a", True)], [_row("b", True)])
