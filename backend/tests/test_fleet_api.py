import asyncio
from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from fleet.api import create_app
from fleet.client import FleetClient
from fleet.models import NewJob
from fleet.policy import OperatorLimits
from fleet.store import claim, publish
from fleet.worker import Worker
from tests.fleet_helpers import sleep_runner


@pytest_asyncio.fixture
async def api(fleet_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=create_app(fleet_engine))
    async with AsyncClient(transport=transport, base_url="http://fleet") as client:
        yield client


def _batch(key: str | None = "k", tag: str = "") -> dict[str, object]:
    jobs = [{"name": f"j{n}", "payload": {"n": n, "tag": tag}} for n in range(3)]
    return {"label": "demo", "idempotency_key": key, "jobs": jobs}


async def test_submitting_a_batch_returns_its_job_ids(api: AsyncClient) -> None:
    response = await api.post("/batches", json=_batch())
    assert response.status_code == 201
    body = response.json()
    assert (body["batch_id"], body["job_ids"], body["created"]) == (1, [1, 2, 3], True)


async def test_resubmitting_the_same_batch_returns_the_same_ids(api: AsyncClient) -> None:
    first = await api.post("/batches", json=_batch())
    again = await api.post("/batches", json=_batch())
    assert again.status_code == 200
    assert again.json()["job_ids"] == first.json()["job_ids"]
    assert again.json()["created"] is False


async def test_reusing_a_key_for_different_jobs_is_a_conflict(api: AsyncClient) -> None:
    await api.post("/batches", json=_batch())
    conflict = await api.post("/batches", json=_batch(tag="other"))
    assert conflict.status_code == 409
    assert "different batch" in conflict.json()["detail"]


async def test_malformed_batches_are_rejected(api: AsyncClient) -> None:
    assert (await api.post("/batches", json={"label": "x", "jobs": []})).status_code == 422
    no_name = {"label": "x", "jobs": [{"name": "", "payload": {}}]}
    assert (await api.post("/batches", json=no_name)).status_code == 422
    no_attempts = {**_batch(), "max_attempts": 0}
    assert (await api.post("/batches", json=no_attempts)).status_code == 422


async def test_a_batch_can_set_its_retry_budget(api: AsyncClient) -> None:
    job_ids = (await api.post("/batches", json={**_batch(), "max_attempts": 5})).json()["job_ids"]
    status = (await api.get(f"/jobs/{job_ids[0]}")).json()
    assert (status["max_attempts"], status["last_error"]) == (5, None)


async def test_status_endpoints_follow_a_job_to_its_result(
    api: AsyncClient, fleet_engine: AsyncEngine
) -> None:
    job_id = (await api.post("/batches", json=_batch())).json()["job_ids"][0]
    assert (await api.get(f"/jobs/{job_id}/result")).status_code == 404
    job = await claim(fleet_engine, worker_id="w", lease_seconds=60)
    assert job is not None and job.id == job_id
    await publish(
        fleet_engine,
        job_id=job.id,
        attempt=1,
        worker_id="w",
        outcome="escalated",
        body={"why": "x"},
    )
    status = (await api.get(f"/jobs/{job_id}")).json()
    assert (status["state"], status["attempt"], status["worker_id"]) == ("escalated", 1, "w")
    result = (await api.get(f"/jobs/{job_id}/result")).json()
    assert (result["outcome"], result["body"]) == ("escalated", {"why": "x"})
    batch = (await api.get("/batches/1")).json()
    assert (batch["counts"], batch["done"]) == ({"escalated": 1, "queued": 2}, False)


async def test_missing_things_are_not_found(api: AsyncClient) -> None:
    for path in ("/batches/9", "/jobs/9", "/jobs/9/result"):
        assert (await api.get(path)).status_code == 404
    assert (await api.get("/health")).json() == {"status": "ok"}


async def test_the_client_submits_and_waits_while_a_worker_drains(
    api: AsyncClient, fleet_engine: AsyncEngine
) -> None:
    client = FleetClient(http=api)
    assert await client.healthy()
    jobs = [NewJob(name=f"j{n}", payload={"sleep_ms": 5}) for n in range(4)]
    submission = await client.submit(label="demo", jobs=jobs, idempotency_key="c")
    worker = Worker(fleet_engine, sleep_runner, worker_id="w", lease_seconds=60)
    draining = asyncio.create_task(worker.run(exit_when_idle=True))
    status = await client.wait_until_done(
        submission.batch_id, poll_seconds=0.02, timeout_seconds=30
    )
    await draining
    assert (status.total, status.counts, status.done) == (4, {"succeeded": 4}, True)


async def test_a_batch_asking_past_the_operators_limits_is_refused(
    fleet_engine: AsyncEngine,
) -> None:
    limits = OperatorLimits(max_memory_mb=512)
    app = create_app(fleet_engine, limits=limits)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fleet") as client:
        too_big = {**_batch(), "policy": {"memory_mb": 4096}}
        refused = await client.post("/batches", json=too_big)
        assert refused.status_code == 403
        assert "memory_mb 4096 is over the limit of 512" in refused.json()["detail"]
        network = {**_batch(key="n"), "policy": {"egress": ["example.com:443"]}}
        assert (await client.post("/batches", json=network)).status_code == 403
        fits = await client.post("/batches", json={**_batch(key="f"), "policy": {"memory_mb": 256}})
        assert fits.status_code == 201
        job = (await client.get(f"/jobs/{fits.json()['job_ids'][0]}")).json()
        assert (job["policy"]["memory_mb"], job["policy"]["egress"]) == (256, [])
