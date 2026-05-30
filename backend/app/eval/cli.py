"""Command-line entrypoint: run the benchmark, persist results, print a summary."""

import argparse
import asyncio

from app.benchmark.loader import load_benchmark
from app.benchmark.schema import Task
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import create_engine, create_session_factory
from app.eval.harness import run_benchmark
from app.eval.report import BenchmarkReport
from app.eval.store import persist_report
from app.llm.base import LLMProvider
from app.llm.factory import build_provider


async def run_eval(
    *,
    label: str,
    limit: int | None = None,
    database_url: str | None = None,
    provider: LLMProvider | None = None,
    create_tables: bool = False,
) -> int:
    """Run the benchmark with the configured (or supplied) provider and persist the report."""
    settings = get_settings()
    tasks = load_benchmark()
    if limit is not None:
        tasks = tasks[:limit]

    active_provider = provider or build_provider(settings)

    def factory(_task: Task) -> LLMProvider:
        return active_provider

    report = await run_benchmark(
        tasks,
        factory,
        label=label,
        provider_name=active_provider.name,
        model=active_provider.model,
    )

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

    _print_summary(report, run_id)
    return run_id


def _print_summary(report: BenchmarkReport, run_id: int) -> None:
    print(f"run #{run_id}  label={report.label}  provider={report.provider}  model={report.model}")
    print(f"solve rate: {report.solve_rate:.1%}  ({report.solved_count}/{report.total_tasks})")
    print(f"avg iterations: {report.average_iterations:.1f}  total tokens: {report.total_tokens}")
    print(f"solve rate by category: {report.solve_rate_by_category()}")
    print(f"failure taxonomy: {report.failure_taxonomy()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the agentic coding benchmark.")
    parser.add_argument("--label", default="run", help="label stored with this benchmark run")
    parser.add_argument("--limit", type=int, default=None, help="run only the first N tasks")
    parser.add_argument(
        "--create-tables",
        action="store_true",
        help="create tables via metadata (instead of relying on alembic migrations)",
    )
    args = parser.parse_args()
    asyncio.run(run_eval(label=args.label, limit=args.limit, create_tables=args.create_tables))


if __name__ == "__main__":
    main()
