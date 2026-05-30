from pathlib import Path

from app.eval.cli import run_eval
from app.llm.mock_provider import MockProvider


async def test_run_eval_persists_a_run(tmp_path: Path) -> None:
    provider = MockProvider(responses=["this is not a tool call"])
    run_id = await run_eval(
        label="cli-smoke",
        limit=1,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}",
        provider=provider,
        create_tables=True,
    )
    assert run_id >= 1
