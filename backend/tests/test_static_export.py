import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.api.static_export import DEFAULT_OUT, DEFAULT_RESULTS, export_static_api, safe_name
from app.db.session import create_engine, create_session_factory
from app.eval.import_results import import_results
from app.main import create_app


def _tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.json"))
    }


async def test_committed_snapshots_match_a_fresh_export(tmp_path: Path) -> None:
    # regenerate with: uv run python -m app.api.static_export
    fresh = tmp_path / "static-api"
    await export_static_api(fresh)
    assert _tree(fresh) == _tree(DEFAULT_OUT)


async def test_every_snapshot_is_exactly_what_the_api_returns(tmp_path: Path) -> None:
    written = await export_static_api(tmp_path / "static-api")
    url = f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"
    for index, path in enumerate(DEFAULT_RESULTS):
        await import_results(path, database_url=url, create_tables=index == 0)
    engine = create_engine(url)
    factory = create_session_factory(engine)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            for file, api_path in written:
                response = await client.get(f"/api{api_path}")
                assert response.status_code == 200, api_path
                snapshot = json.loads((tmp_path / "static-api" / file).read_text())
                assert snapshot == response.json(), file
    finally:
        await engine.dispose()


async def test_export_covers_every_run_task_and_label_pair(tmp_path: Path) -> None:
    files = [file for file, _ in await export_static_api(tmp_path / "static-api")]
    assert "runs.json" in files
    assert sum(1 for f in files if f.startswith("runs/") and f.count("/") == 1) == 2
    assert sum(1 for f in files if "/tasks/" in f) == 30
    assert sum(1 for f in files if f.startswith("compare/")) == 4


def test_names_that_would_not_map_onto_a_url_are_refused() -> None:
    assert safe_name("groq-llama-3.3-70b") == "groq-llama-3.3-70b"
    with pytest.raises(ValueError, match="static file name"):
        safe_name("a/b")
