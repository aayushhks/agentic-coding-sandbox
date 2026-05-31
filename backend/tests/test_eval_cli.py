import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.types import AgentConfig
from app.db.models import TaskResult
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


async def test_run_eval_threads_require_verified_finish(tmp_path: Path) -> None:
    # a provider that only ever tries to finish never authors a test, so under the gate the task
    # ends in exhausted iterations instead of a premature (and unverified) finish
    finish = json.dumps({"thought": "done", "tool": "finish", "arguments": {"answer": "x"}})
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'hardened.db'}"
    run_id = await run_eval(
        label="hardened",
        limit=1,
        database_url=db_url,
        provider=MockProvider(responses=[finish] * 20),
        agent_config=AgentConfig(require_verified_finish=True),
        create_tables=True,
    )

    engine = create_async_engine(db_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            rows = (
                (await session.execute(select(TaskResult).where(TaskResult.run_id == run_id)))
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()

    assert len(rows) == 1
    assert not rows[0].solved
    assert rows[0].failure_mode == "exhausted_iterations"
