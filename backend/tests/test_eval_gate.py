import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.eval.compare import TaskRow, compare_runs
from app.eval.failure import FailureMode
from app.eval.gate import evaluate_gate, load_baseline_rows
from app.eval.gate_cli import run_gate
from app.eval.report import BenchmarkReport, TaskOutcome
from app.eval.store import persist_report

BASELINE_V2 = (
    Path(__file__).resolve().parents[2] / "docs" / "results" / "groq-llama-3.3-70b-v2.json"
)


def _rows(specs: Sequence[tuple[str, bool]]) -> list[TaskRow]:
    return [
        TaskRow(
            task_id=task_id,
            category="algorithms",
            difficulty="easy",
            solved=solved,
            failure_mode=None if solved else "wrong_solution",
            iterations=3,
            total_tokens=100,
        )
        for task_id, solved in specs
    ]


def test_load_baseline_rows_parses_committed_v2() -> None:
    label, rows = load_baseline_rows(BASELINE_V2)
    assert label == "groq-llama-3.3-70b-v2-hardened"
    assert len(rows) == 15
    assert all(row.solved for row in rows)  # v2 is 100%
    by_id = {row.task_id: row for row in rows}
    assert by_id["lru_cache"].solved is True


def test_gate_passes_with_no_regression_above_floor() -> None:
    comparison = compare_runs(
        "base", "cand", _rows([("a", True), ("b", False)]), _rows([("a", True), ("b", True)])
    )
    outcome = evaluate_gate(comparison, min_solve_rate=1.0)
    assert outcome.passed
    assert outcome.reasons == []
    assert outcome.candidate_solve_rate == 1.0


def test_gate_fails_on_regression() -> None:
    comparison = compare_runs(
        "base", "cand", _rows([("a", True), ("b", True)]), _rows([("a", True), ("b", False)])
    )
    outcome = evaluate_gate(comparison, min_solve_rate=0.0)
    assert not outcome.passed
    assert any("regressed" in reason and "b" in reason for reason in outcome.reasons)


def test_gate_fails_below_solve_rate_floor() -> None:
    comparison = compare_runs(
        "base", "cand", _rows([("a", False), ("b", False)]), _rows([("a", True), ("b", False)])
    )
    outcome = evaluate_gate(comparison, min_solve_rate=1.0)
    assert not outcome.passed
    assert any("floor" in reason for reason in outcome.reasons)


def _outcome(task_id: str, *, solved: bool) -> TaskOutcome:
    return TaskOutcome(
        task_id=task_id,
        category="algorithms",
        difficulty="easy",
        solved=solved,
        iterations=3,
        prompt_tokens=80,
        completion_tokens=20,
        total_tokens=100,
        wall_clock_seconds=1.0,
        failure_mode=None if solved else FailureMode.WRONG_SOLUTION,
        tool_counts={"finish": 1},
        trace=[],
        eval_exit_code=0 if solved else 1,
        eval_output="",
    )


def _baseline_json(path: Path, specs: Sequence[tuple[str, bool]]) -> Path:
    tasks: list[dict[str, Any]] = [
        {
            "task_id": task_id,
            "category": "algorithms",
            "difficulty": "easy",
            "solved": solved,
            "failure_mode": None if solved else "wrong_solution",
            "iterations": 3,
            "total_tokens": 100,
        }
        for task_id, solved in specs
    ]
    path.write_text(json.dumps({"label": "baseline", "tasks": tasks}))
    return path


async def _seed_candidate(db_url: str, specs: Sequence[tuple[str, bool]]) -> None:
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await persist_report(
            session,
            BenchmarkReport(
                label="cand",
                provider="mock",
                model="mock-model",
                version="v1",
                outcomes=[_outcome(task_id, solved=solved) for task_id, solved in specs],
            ),
        )
    await engine.dispose()


async def test_run_gate_passes_when_candidate_matches_baseline(tmp_path: Path) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'gate.db'}"
    await _seed_candidate(db_url, [("a", True), ("b", True)])
    baseline = _baseline_json(tmp_path / "baseline.json", [("a", True), ("b", True)])

    outcome = await run_gate(
        baseline_path=baseline, candidate_label="cand", min_solve_rate=1.0, database_url=db_url
    )
    assert outcome.passed


async def test_run_gate_fails_when_candidate_regresses(tmp_path: Path) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'gate.db'}"
    await _seed_candidate(db_url, [("a", True), ("b", False)])
    baseline = _baseline_json(tmp_path / "baseline.json", [("a", True), ("b", True)])

    outcome = await run_gate(
        baseline_path=baseline, candidate_label="cand", min_solve_rate=0.0, database_url=db_url
    )
    assert not outcome.passed
    assert outcome.comparison.regressed[0].task_id == "b"
