"""CLI: diff two persisted benchmark runs and exit non-zero if any task regressed."""

import argparse
import asyncio

from app.db.session import create_engine, create_session_factory
from app.eval.compare import RunComparison, compare_runs
from app.eval.store import load_run_rows


async def run_compare(
    baseline_label: str,
    candidate_label: str,
    database_url: str | None = None,
) -> RunComparison:
    """Load both runs from the database by label and compare them."""
    engine = create_engine(database_url)
    try:
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            baseline_name, baseline_rows = await load_run_rows(session, baseline_label)
            candidate_name, candidate_rows = await load_run_rows(session, candidate_label)
    finally:
        await engine.dispose()
    return compare_runs(baseline_name, candidate_name, baseline_rows, candidate_rows)


def print_comparison(comparison: RunComparison) -> None:
    print(f"baseline={comparison.baseline_label}  candidate={comparison.candidate_label}")
    print(
        f"solve rate: {comparison.baseline_solve_rate:.1%} -> "
        f"{comparison.candidate_solve_rate:.1%}  ({comparison.solve_rate_delta:+.1%})"
    )
    converted = [d.task_id for d in comparison.converted]
    regressed = [d.task_id for d in comparison.regressed]
    print(f"converted (fail -> pass): {converted or 'none'}")
    print(f"regressed (pass -> fail): {regressed or 'none'}")
    print(f"total token delta: {comparison.token_delta:+d}")
    print()

    header = f"{'task':<20}{'base -> cand':<16}{'transition':<16}{'Δiters':>8}{'Δtokens':>10}"
    print(header)
    print("-" * len(header))
    for delta in comparison.deltas:
        base = "pass" if delta.baseline_solved else "fail"
        cand = "pass" if delta.candidate_solved else "fail"
        print(
            f"{delta.task_id:<20}{f'{base} -> {cand}':<16}{delta.transition.value:<16}"
            f"{delta.iteration_delta:>+8}{delta.token_delta:>+10}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Diff two benchmark runs by label; exit 1 if any task regressed."
    )
    parser.add_argument("--baseline", required=True, help="label of the baseline run")
    parser.add_argument("--candidate", required=True, help="label of the candidate run")
    args = parser.parse_args()

    comparison = asyncio.run(run_compare(args.baseline, args.candidate))
    print_comparison(comparison)
    if comparison.has_regression:
        regressed = ", ".join(d.task_id for d in comparison.regressed)
        print(f"\nREGRESSION: {len(comparison.regressed)} task(s) regressed: {regressed}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
