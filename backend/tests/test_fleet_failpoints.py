"""Failpoints do nothing until armed, then fire once, at their hit, and log what they did first."""

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from fleet import failpoints
from fleet.failpoints import ARMED_VARIABLE, LOG_VARIABLE, Armed, FailpointError, arm, failpoint
from fleet.models import NewJob
from fleet.store import claim, job_result, job_status, publish, start, submit_batch
from fleet.worker import Worker
from tests.fleet_helpers import BACKEND_ROOT, sleep_runner


@pytest.fixture(autouse=True)
def _disarmed() -> Iterator[None]:
    yield
    arm("")


def test_a_spec_arms_points_with_an_action_and_the_hit_to_fire_on() -> None:
    assert failpoints.parse("claim.before=kill@2, publish.before_commit=drop@1") == {
        "claim.before": Armed("kill", 2),
        "publish.before_commit": Armed("drop", 1),
    }
    assert failpoints.parse("") == {}
    for spec in ("nowhere=kill@1", "claim.before=explode@1", "claim.before=kill@0", "claim.before"):
        with pytest.raises(FailpointError):
            failpoints.parse(spec)


async def test_an_unarmed_point_does_nothing_and_an_armed_one_fires_once() -> None:
    await failpoint("publish.after_commit")
    arm("publish.after_commit=drop@2")
    await failpoint("publish.after_commit")
    # after the commit there is no transaction to end, so the answer is what gets lost
    with pytest.raises(ConnectionResetError):
        await failpoint("publish.after_commit")
    await failpoint("publish.after_commit")


def _armed_process(spec: str, log: Path, script: str) -> "subprocess.Popen[str]":
    return subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=BACKEND_ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(BACKEND_ROOT),
            ARMED_VARIABLE: spec,
            LOG_VARIABLE: str(log),
        },
        stdout=subprocess.PIPE,
        text=True,
    )


HITS = """
import asyncio
from fleet.failpoints import failpoint

async def main():
    for n in (1, 2, 3):
        print("hit", n, flush=True)
        await failpoint("claim.before", worker="w7")
    print("done", flush=True)

asyncio.run(main())
"""


def test_kill_logs_the_fault_then_kills_the_process_at_its_hit(tmp_path: Path) -> None:
    log = tmp_path / "faults.jsonl"
    process = _armed_process("claim.before=kill@2", log, HITS)
    output, _ = process.communicate(timeout=30)
    assert process.returncode == -signal.SIGKILL
    assert output.split("\n")[:2] == ["hit 1", "hit 2"] and "hit 3" not in output
    (fired,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert (fired["point"], fired["action"], fired["worker"]) == ("claim.before", "kill", "w7")
    assert fired["pid"] == process.pid


def _state(pid: int) -> str:
    stat = Path(f"/proc/{pid}/stat").read_text()
    return stat.rsplit(")", 1)[1].split()[0]


def test_stop_waits_for_whoever_reads_the_log_to_resume_it(tmp_path: Path) -> None:
    log = tmp_path / "faults.jsonl"
    process = _armed_process("claim.before=stop@1", log, HITS)
    deadline = time.monotonic() + 30
    while not log.exists() or _state(process.pid) != "T":
        assert time.monotonic() < deadline, "the process never stopped"
        time.sleep(0.01)
    process.send_signal(signal.SIGCONT)
    output, _ = process.communicate(timeout=30)
    assert process.returncode == 0
    assert output.split() == ["hit", "1", "hit", "2", "hit", "3", "done"]


async def test_drop_inside_a_transaction_ends_the_connection_and_the_transaction(
    fleet_engine: AsyncEngine,
) -> None:
    arm("claim.before_commit=drop@1")
    with pytest.raises(DBAPIError) as raised:
        async with fleet_engine.begin() as connection:
            await connection.execute(
                text(
                    "insert into fleet_batches (request_digest, label) "
                    "values ('digest', 'dropped before commit')"
                )
            )
            await failpoint("claim.before_commit", connection)
    assert raised.value.connection_invalidated
    async with fleet_engine.connect() as connection:
        # nothing it wrote survived, and the next connection works
        count = await connection.scalar(
            text("select count(*) from fleet_batches where label = 'dropped before commit'")
        )
    assert count == 0


async def test_a_job_meets_the_points_in_protocol_order(
    fleet_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    met: list[str] = []

    async def note(point: str, connection: object = None, **context: object) -> None:
        met.append(point)

    monkeypatch.setattr("fleet.store.failpoint", note)
    await submit_batch(
        fleet_engine, label="points", jobs=[NewJob(name="j", payload={"sleep_ms": 250})]
    )
    worker = Worker(
        fleet_engine, sleep_runner, worker_id="w", lease_seconds=0.6, heartbeat_seconds=0.1
    )
    assert await worker.step()
    beats = met.count("heartbeat.before")
    assert beats >= 1
    assert met == [
        "claim.before",
        "claim.before_commit",
        "claim.after_commit",
        "start.after_commit",
        *["heartbeat.before"] * beats,
        "publish.before_commit",
        "publish.after_commit",
    ]


async def test_what_survives_a_drop_depends_on_which_side_of_the_commit_it_lands(
    fleet_engine: AsyncEngine,
) -> None:
    submission = await submit_batch(
        fleet_engine, label="sides", jobs=[NewJob(name="j", payload={})]
    )
    job_id = submission.job_ids[0]
    arm("claim.before_commit=drop@1")
    with pytest.raises(DBAPIError):
        await claim(fleet_engine, worker_id="w", lease_seconds=60)
    # rolled back: the job waits in the queue, and no attempt was logged
    status = await job_status(fleet_engine, job_id)
    assert status is not None and (status.state, status.attempt) == ("queued", 0)
    arm("claim.after_commit=drop@1")
    with pytest.raises(ConnectionResetError):
        await claim(fleet_engine, worker_id="w", lease_seconds=60)
    # committed: the job is held, though the claimer never heard back
    status = await job_status(fleet_engine, job_id)
    assert status is not None and (status.state, status.attempt) == ("claimed", 1)
    assert await start(fleet_engine, job_id=job_id, attempt=1)
    arm("publish.before_commit=drop@1")

    async def publishing() -> bool:
        return await publish(
            fleet_engine, job_id=job_id, attempt=1, worker_id="w", outcome="succeeded", body={}
        )

    with pytest.raises(DBAPIError):
        await publishing()
    assert await job_result(fleet_engine, job_id) is None
    arm("publish.after_commit=drop@1")
    with pytest.raises(ConnectionResetError):
        await publishing()
    result = await job_result(fleet_engine, job_id)
    assert result is not None and result.attempt == 1
