from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import create_app

EXPECTED_STAT_KEYS = {
    "accuracy",
    "resolution_rate",
    "correct_escalation_rate",
    "false_fix_rate",
    "injection_resistance",
    "mean_iterations",
    "total_tokens",
    "tokens_per_ticket",
    "total_wall_clock_seconds",
    "latency_seconds_per_ticket",
    "failure_taxonomy",
}


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """get_settings is lru_cached; clear it around each test so env overrides take effect."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def api() -> AsyncIterator[AsyncClient]:
    """An httpx client bound to the app; the report path defaults to the committed reference."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_deployment_report_returns_reference_report(api: AsyncClient) -> None:
    response = await api.get("/api/deployment-report")
    assert response.status_code == 200
    body = response.json()
    assert set(body["stats"]) == EXPECTED_STAT_KEYS
    assert body["outcomes"]
    first = body["outcomes"][0]
    assert first["ticket_id"] == "TCK-01"
    assert "trace" in first


async def test_deployment_report_missing_returns_404(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DEPLOYMENT_REPORT_PATH", str(tmp_path / "missing.json"))
    get_settings.cache_clear()
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/deployment-report")).status_code == 404
