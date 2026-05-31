"""Compare two benchmark runs to find converted and regressed tasks."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class Transition(StrEnum):
    CONVERTED = "converted"  # failed in the baseline, passes in the candidate
    REGRESSED = "regressed"  # passed in the baseline, fails in the candidate
    UNCHANGED_PASS = "unchanged_pass"
    UNCHANGED_FAIL = "unchanged_fail"


@dataclass(frozen=True, slots=True)
class TaskRow:
    """The minimal per-task fields needed to diff one run against another."""

    task_id: str
    category: str
    difficulty: str
    solved: bool
    failure_mode: str | None
    iterations: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class TaskDelta:
    task_id: str
    category: str
    difficulty: str
    transition: Transition
    baseline_solved: bool
    candidate_solved: bool
    baseline_failure: str | None
    candidate_failure: str | None
    iteration_delta: int
    token_delta: int


@dataclass(frozen=True, slots=True)
class RunComparison:
    baseline_label: str
    candidate_label: str
    deltas: list[TaskDelta]

    @property
    def converted(self) -> list[TaskDelta]:
        return [d for d in self.deltas if d.transition == Transition.CONVERTED]

    @property
    def regressed(self) -> list[TaskDelta]:
        return [d for d in self.deltas if d.transition == Transition.REGRESSED]

    @property
    def has_regression(self) -> bool:
        return bool(self.regressed)

    @property
    def total_tasks(self) -> int:
        return len(self.deltas)

    @property
    def baseline_solved_count(self) -> int:
        return sum(1 for d in self.deltas if d.baseline_solved)

    @property
    def candidate_solved_count(self) -> int:
        return sum(1 for d in self.deltas if d.candidate_solved)

    @property
    def baseline_solve_rate(self) -> float:
        return self.baseline_solved_count / self.total_tasks if self.deltas else 0.0

    @property
    def candidate_solve_rate(self) -> float:
        return self.candidate_solved_count / self.total_tasks if self.deltas else 0.0

    @property
    def solve_rate_delta(self) -> float:
        return self.candidate_solve_rate - self.baseline_solve_rate

    @property
    def token_delta(self) -> int:
        return sum(d.token_delta for d in self.deltas)


def _transition(baseline_solved: bool, candidate_solved: bool) -> Transition:
    if baseline_solved and candidate_solved:
        return Transition.UNCHANGED_PASS
    if not baseline_solved and not candidate_solved:
        return Transition.UNCHANGED_FAIL
    return Transition.CONVERTED if candidate_solved else Transition.REGRESSED


def compare_runs(
    baseline_label: str,
    candidate_label: str,
    baseline: Sequence[TaskRow],
    candidate: Sequence[TaskRow],
) -> RunComparison:
    """Diff two runs task by task, rejecting runs that do not cover the same set of task ids."""
    baseline_by_id = {row.task_id: row for row in baseline}
    candidate_by_id = {row.task_id: row for row in candidate}
    if baseline_by_id.keys() != candidate_by_id.keys():
        only_baseline = sorted(baseline_by_id.keys() - candidate_by_id.keys())
        only_candidate = sorted(candidate_by_id.keys() - baseline_by_id.keys())
        raise ValueError(
            f"runs cover different tasks: only in baseline={only_baseline}, "
            f"only in candidate={only_candidate}"
        )
    deltas: list[TaskDelta] = []
    for task_id in sorted(baseline_by_id):
        base = baseline_by_id[task_id]
        cand = candidate_by_id[task_id]
        deltas.append(
            TaskDelta(
                task_id=task_id,
                category=base.category,
                difficulty=base.difficulty,
                transition=_transition(base.solved, cand.solved),
                baseline_solved=base.solved,
                candidate_solved=cand.solved,
                baseline_failure=base.failure_mode,
                candidate_failure=cand.failure_mode,
                iteration_delta=cand.iterations - base.iterations,
                token_delta=cand.total_tokens - base.total_tokens,
            )
        )
    return RunComparison(baseline_label, candidate_label, deltas)
