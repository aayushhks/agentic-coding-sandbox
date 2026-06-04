"""CLI: gate a candidate run (by label) against a committed baseline; exit 1 if it fails."""

import argparse
import asyncio
from pathlib import Path

from app.db.session import create_engine, create_session_factory
from app.eval.compare import compare_runs
from app.eval.gate import GateOutcome, evaluate_gate, load_baseline_rows
from app.eval.store import load_run_rows


async def run_gate(
    *,
    baseline_path: Path,
    candidate_label: str,
    min_solve_rate: float,
    database_url: str | None = None,
) -> GateOutcome:
    """Compare the latest candidate run against the baseline JSON and apply the gate rules."""
    baseline_label, baseline_rows = load_baseline_rows(baseline_path)
    engine = create_engine(database_url)
    try:
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            candidate_label, candidate_rows = await load_run_rows(session, candidate_label)
    finally:
        await engine.dispose()
    comparison = compare_runs(baseline_label, candidate_label, baseline_rows, candidate_rows)
    return evaluate_gate(comparison, min_solve_rate)


def print_outcome(outcome: GateOutcome) -> None:
    comparison = outcome.comparison
    print(f"baseline={comparison.baseline_label}  candidate={comparison.candidate_label}")
    print(
        f"solve rate: {comparison.baseline_solve_rate:.1%} -> "
        f"{comparison.candidate_solve_rate:.1%}  (floor {outcome.min_solve_rate:.1%})"
    )
    print(f"converted: {[d.task_id for d in comparison.converted] or 'none'}")
    print(f"regressed: {[d.task_id for d in comparison.regressed] or 'none'}")
    if outcome.passed:
        print("GATE PASSED")
    else:
        print("GATE FAILED:")
        for reason in outcome.reasons:
            print(f"  - {reason}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gate a candidate run against a committed baseline; exit 1 on regression."
    )
    parser.add_argument(
        "--baseline", required=True, type=Path, help="path to a committed results JSON"
    )
    parser.add_argument(
        "--candidate", required=True, help="label of the candidate run in the database"
    )
    parser.add_argument(
        "--min-solve-rate",
        type=float,
        default=0.0,
        help="minimum acceptable candidate solve rate, 0..1 (default: 0.0)",
    )
    args = parser.parse_args()

    outcome = asyncio.run(
        run_gate(
            baseline_path=args.baseline,
            candidate_label=args.candidate,
            min_solve_rate=args.min_solve_rate,
        )
    )
    print_outcome(outcome)
    if not outcome.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
