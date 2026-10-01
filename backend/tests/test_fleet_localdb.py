"""A local cluster restarted the way a crash would keeps its commits and takes callers back."""

import asyncio
import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from fleet.connections import ride_through
from fleet.localdb import LocalPostgres, PostgresUnavailableError
from fleet.migrate import migrate
from fleet.models import NewJob
from fleet.store import batch_status, submit_batch


async def test_a_cluster_restarted_like_a_crash_keeps_what_was_committed() -> None:
    try:
        cluster = LocalPostgres.start()
    except PostgresUnavailableError as exc:
        if os.environ.get("REQUIRE_POSTGRES") == "1":
            pytest.fail(f"a local cluster is required but unavailable: {exc}")
        pytest.skip(str(exc))
    with cluster:
        await asyncio.to_thread(migrate, cluster.url)
        engine = create_async_engine(cluster.url)
        try:
            jobs = [NewJob(name=f"j{n}", payload={}) for n in range(2)]
            submission = await submit_batch(engine, label="committed", jobs=jobs)
            before = cluster.pid
            assert before is not None
            # signal 0 only checks that the process is there
            os.kill(before, 0)
            await asyncio.to_thread(cluster.restart)
            assert cluster.pid not in (None, before)
            # the pool's connections died with the server, so the next call needs a fresh one
            status = await ride_through(
                lambda: batch_status(engine, submission.batch_id), seconds=10
            )
            assert status is not None and status.total == 2
        finally:
            await engine.dispose()
    assert cluster.pid is None
