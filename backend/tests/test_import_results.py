from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import BenchmarkRun
from app.eval.import_results import import_results, report_from_results_json

V2_RESULTS = Path(__file__).resolve().parents[2] / "docs" / "results" / "groq-llama-3.3-70b-v2.json"


def test_report_from_results_json_reconstructs_run() -> None:
    report = report_from_results_json(V2_RESULTS)
    assert report.label == "groq-llama-3.3-70b-v2-hardened"
    assert report.total_tasks == 15
    assert report.solved_count == 15
    assert report.solve_rate == 1.0
    assert report.failure_taxonomy() == {}


async def test_import_results_persists_a_run(tmp_path: Path) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'imp.db'}"
    run_id = await import_results(V2_RESULTS, database_url=db_url, create_tables=True)
    assert run_id >= 1

    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        run = (
            await session.execute(select(BenchmarkRun).where(BenchmarkRun.id == run_id))
        ).scalar_one()
        assert run.label == "groq-llama-3.3-70b-v2-hardened"
        assert run.solved_count == 15
        assert run.solve_rate == 1.0
        assert run.stats["solve_rate_by_category"]["bugfix"] == 1.0
    await engine.dispose()
