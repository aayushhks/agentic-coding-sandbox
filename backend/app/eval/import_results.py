"""Import a committed results JSON into the database as a benchmark run.

The agent's sandbox needs ``unshare --net`` privileges that managed hosts (Railway, Vercel,
…) don't grant, so a deployed instance can't *produce* runs — it can only display them. This
seeds a deployed database from a results file checked into ``docs/results/`` so the dashboard
has something to show. Per-task traces are not stored in the results JSON, so imported runs
have empty traces (the solve rates, taxonomy, tokens, and diff are all intact).
"""

import argparse
import asyncio
import json
from pathlib import Path

from app.db.base import Base
from app.db.session import create_engine, create_session_factory
from app.eval.failure import FailureMode
from app.eval.report import BenchmarkReport, TaskOutcome
from app.eval.store import persist_report


def report_from_results_json(path: Path) -> BenchmarkReport:
    """Rebuild a BenchmarkReport from a results JSON written under docs/results/."""
    data = json.loads(Path(path).read_text())
    outcomes = [
        TaskOutcome(
            task_id=str(task["task_id"]),
            category=str(task["category"]),
            difficulty=str(task["difficulty"]),
            solved=bool(task["solved"]),
            iterations=int(task.get("iterations", 0)),
            prompt_tokens=int(task.get("prompt_tokens", 0)),
            completion_tokens=int(task.get("completion_tokens", 0)),
            total_tokens=int(task.get("total_tokens", 0)),
            wall_clock_seconds=float(task.get("wall_clock_seconds", 0.0)),
            failure_mode=(
                None if task.get("failure_mode") is None else FailureMode(task["failure_mode"])
            ),
            tool_counts=dict(task.get("tool_counts", {})),
            trace=[],  # traces are not captured in the results JSON
            eval_exit_code=0 if task["solved"] else 1,
            eval_output="",
        )
        for task in data["tasks"]
    ]
    return BenchmarkReport(
        label=str(data["label"]),
        provider=str(data["provider"]),
        model=str(data["model"]),
        version=str(data.get("benchmark_version", "v1")),
        outcomes=outcomes,
    )


async def import_results(
    path: Path, database_url: str | None = None, create_tables: bool = False
) -> int:
    """Persist the run described by a results JSON and return the new run id."""
    report = report_from_results_json(path)
    engine = create_engine(database_url)
    try:
        if create_tables:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            run_id = await persist_report(session, report)
    finally:
        await engine.dispose()
    return run_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a results JSON into the database.")
    parser.add_argument("--results", required=True, type=Path, help="path to a results JSON file")
    parser.add_argument(
        "--create-tables",
        action="store_true",
        help="create tables via metadata (instead of relying on alembic migrations)",
    )
    args = parser.parse_args()
    run_id = asyncio.run(import_results(args.results, create_tables=args.create_tables))
    print(f"imported {args.results} as run #{run_id}")


if __name__ == "__main__":
    main()
