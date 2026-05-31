from pathlib import Path

from app.eval.compare import RunComparison, TaskDelta, Transition
from app.eval.plots import write_comparison_figures


def _delta(
    task_id: str,
    transition: Transition,
    *,
    baseline_solved: bool = True,
    candidate_solved: bool = True,
    token_delta: int = 100,
) -> TaskDelta:
    return TaskDelta(
        task_id=task_id,
        category="algorithms",
        difficulty="easy",
        transition=transition,
        baseline_solved=baseline_solved,
        candidate_solved=candidate_solved,
        baseline_failure=None,
        candidate_failure=None,
        iteration_delta=1,
        token_delta=token_delta,
    )


def test_write_comparison_figures(tmp_path: Path) -> None:
    deltas = [
        _delta("a", Transition.CONVERTED, baseline_solved=False, token_delta=300),
        _delta("b", Transition.REGRESSED, candidate_solved=False, token_delta=-50),
        _delta("c", Transition.UNCHANGED_PASS, token_delta=80),
    ]
    written = write_comparison_figures(RunComparison("v1", "v2", deltas), tmp_path)

    assert len(written) == 2
    for path in written:
        assert path.exists()
        assert path.stat().st_size > 0
