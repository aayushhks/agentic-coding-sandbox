"""Gate a candidate benchmark run against a committed baseline (used by CI).

The gate fails when a candidate run regresses against a known-good baseline: any task that
passed in the baseline but fails in the candidate, or an overall solve rate below a floor.
The baseline is a committed results JSON (the format written under ``docs/results/``), so the
expected behavior is version-controlled alongside the code.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.eval.compare import RunComparison, TaskRow


def load_baseline_rows(path: Path) -> tuple[str, list[TaskRow]]:
    """Load a committed results JSON into a label and per-task comparison rows."""
    data: dict[str, Any] = json.loads(Path(path).read_text())
    label = str(data.get("label", path.name))
    rows = [
        TaskRow(
            task_id=str(task["task_id"]),
            category=str(task["category"]),
            difficulty=str(task["difficulty"]),
            solved=bool(task["solved"]),
            failure_mode=(None if task.get("failure_mode") is None else str(task["failure_mode"])),
            iterations=int(task.get("iterations", 0)),
            total_tokens=int(task.get("total_tokens", 0)),
        )
        for task in data["tasks"]
    ]
    return label, rows


@dataclass(slots=True)
class GateOutcome:
    """The verdict of gating a candidate comparison, plus why it failed."""

    passed: bool
    reasons: list[str]
    comparison: RunComparison
    min_solve_rate: float

    @property
    def candidate_solve_rate(self) -> float:
        return self.comparison.candidate_solve_rate


def evaluate_gate(comparison: RunComparison, min_solve_rate: float) -> GateOutcome:
    """Fail the gate on any per-task regression or a solve rate below the floor."""
    reasons: list[str] = []
    if comparison.has_regression:
        regressed = ", ".join(d.task_id for d in comparison.regressed)
        reasons.append(f"{len(comparison.regressed)} task(s) regressed: {regressed}")
    if comparison.candidate_solve_rate < min_solve_rate:
        reasons.append(
            f"solve rate {comparison.candidate_solve_rate:.1%} is below the floor "
            f"{min_solve_rate:.1%}"
        )
    return GateOutcome(
        passed=not reasons,
        reasons=reasons,
        comparison=comparison,
        min_solve_rate=min_solve_rate,
    )
