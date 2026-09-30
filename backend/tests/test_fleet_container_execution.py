"""Jobs run in containers of their own: the limits, the fences and the cleanup, tested for real."""

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from fleet.containers import MANAGED, labels, task_config
from fleet.docker import Docker
from fleet.execution import DEPLOYMENT, InContainer
from fleet.models import NewJob, PublishedResult
from fleet.policy import ExecutionPolicy
from fleet.store import cancel, claim, job_result, job_status, live_attempts, submit_batch
from fleet.worker import Worker
from tests.fleet_helpers import NO_BACKOFF

PROBES = "fleet.probes:run"


@pytest_asyncio.fixture
async def docker(task_image: str) -> AsyncIterator[Docker]:
    client = Docker()
    try:
        yield client
    finally:
        await client.aclose()


def _execution(docker: Docker, image: str, deployment: str, **options: Any) -> InContainer:
    return InContainer(docker, image=image, runner=PROBES, deployment=deployment, **options)


async def _run_probe(
    engine: AsyncEngine,
    execution: InContainer,
    payload: dict[str, Any],
    policy: ExecutionPolicy | None = None,
    *,
    max_attempts: int = 3,
) -> tuple[int, PublishedResult | None]:
    submission = await submit_batch(
        engine,
        label="probe",
        jobs=[NewJob(name=payload["probe"], payload=payload)],
        policy=policy or ExecutionPolicy(),
        max_attempts=max_attempts,
    )
    await execution.ready()
    worker = Worker(engine, execution, worker_id="w", lease_seconds=30, retry=NO_BACKOFF)
    assert await worker.step()
    job_id = submission.job_ids[0]
    return job_id, await job_result(engine, job_id)


async def _execution_record(engine: AsyncEngine, job_id: int) -> dict[str, Any]:
    async with engine.connect() as connection:
        raw = await connection.scalar(
            text("select execution from fleet_attempts where job_id = :job order by attempt desc"),
            {"job": job_id},
        )
    loaded: dict[str, Any] = json.loads(raw) if isinstance(raw, str) else raw
    return loaded


async def _containers(docker: Docker, deployment: str) -> list[dict[str, Any]]:
    return await docker.containers({MANAGED: "1", DEPLOYMENT: deployment})


async def test_a_job_runs_in_its_container_and_the_attempt_records_how(
    fleet_engine: AsyncEngine, docker: Docker, task_image: str, deployment: str
) -> None:
    execution = _execution(docker, task_image, deployment)
    policy = ExecutionPolicy(cpus=0.5, memory_mb=256)
    job_id, result = await _run_probe(
        fleet_engine, execution, {"probe": "sleep", "seconds": 0.3}, policy
    )
    assert result is not None and (result.outcome, result.body["steps"]) == ("succeeded", 3)
    record = await _execution_record(fleet_engine, job_id)
    assert (record["mode"], record["exit_code"], record["network"]) == ("container", 0, "none")
    assert record["image_id"].startswith("sha256:")
    assert (record["policy"]["cpus"], record["policy"]["memory_mb"]) == (0.5, 256)
    assert record["usage"]["max_rss_mb"] > 0
    # nothing is left behind once the attempt is over
    assert await _containers(docker, deployment) == []


async def test_a_task_past_its_memory_limit_is_killed_and_reported_as_such(
    fleet_engine: AsyncEngine, docker: Docker, task_image: str, deployment: str
) -> None:
    execution = _execution(docker, task_image, deployment)
    job_id, result = await _run_probe(
        fleet_engine,
        execution,
        {"probe": "allocate", "mb": 400},
        ExecutionPolicy(memory_mb=128),
    )
    assert result is not None and result.outcome == "failed"
    assert (result.body["failure"], result.attempt) == ("memory_limit", 1)
    assert result.body["policy"]["memory_mb"] == 128
    record = await _execution_record(fleet_engine, job_id)
    assert record["oom_killed"] and record["exit_code"] != 0


async def test_a_task_past_its_timeout_is_stopped_and_keeps_its_partial_result(
    fleet_engine: AsyncEngine, docker: Docker, task_image: str, deployment: str
) -> None:
    execution = _execution(docker, task_image, deployment)
    started = time.monotonic()
    job_id, result = await _run_probe(
        fleet_engine,
        execution,
        {"probe": "sleep", "seconds": 30},
        ExecutionPolicy(timeout_seconds=3),
    )
    assert time.monotonic() - started < 15
    assert result is not None and (result.outcome, result.body["failure"]) == ("failed", "timeout")
    # the steps it reported before the deadline survive the kill
    assert result.body["partial"]["events"] >= 5
    assert result.body["partial"]["last"][0] == {"step": 0}
    assert (await _execution_record(fleet_engine, job_id))["timed_out"]
    assert await _containers(docker, deployment) == []


async def test_a_task_gets_the_cpu_its_policy_allows_and_no_more(
    fleet_engine: AsyncEngine, docker: Docker, task_image: str, deployment: str
) -> None:
    execution = _execution(docker, task_image, deployment)
    _, result = await _run_probe(
        fleet_engine, execution, {"probe": "busy", "seconds": 2}, ExecutionPolicy(cpus=0.5)
    )
    assert result is not None
    share = result.body["cpu"] / result.body["wall"]
    assert 0.3 < share < 0.6, share


async def test_a_task_with_no_grant_has_no_network(
    fleet_engine: AsyncEngine, docker: Docker, task_image: str, deployment: str
) -> None:
    execution = _execution(docker, task_image, deployment)
    _, result = await _run_probe(
        fleet_engine, execution, {"probe": "connect", "host": "1.1.1.1", "port": 443}
    )
    assert result is not None
    assert result.body["direct"].startswith("refused")
    assert result.body["proxy"] == "no proxy"


async def test_generated_code_cannot_reach_the_network_the_control_files_or_the_task(
    fleet_engine: AsyncEngine, docker: Docker, task_image: str, deployment: str
) -> None:
    execution = _execution(docker, task_image, deployment)
    commands = {
        "network": "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 443), 2)\"",
        "control_files": "ls -A /in /out",
        "environment": "cat /proc/{runner}/environ",
        "files": "ls /proc/{runner}/root/out",
        "signal": "kill -0 {runner} && echo reached || echo unreachable",
    }
    _, result = await _run_probe(
        fleet_engine, execution, {"probe": "sandbox", "commands": commands}
    )
    assert result is not None
    seen = result.body
    assert seen["isolation"] == "user+net+mount+pid"
    assert "Network is unreachable" in seen["network"]
    assert seen["control_files"].split() == ["/in:", "/out:"]
    assert "Permission denied" in seen["environment"]
    assert "Permission denied" in seen["files"]
    assert seen["signal"].endswith("unreachable")


async def test_a_runner_that_raises_in_its_container_is_retried_then_dead_lettered(
    fleet_engine: AsyncEngine, docker: Docker, task_image: str, deployment: str
) -> None:
    execution = _execution(docker, task_image, deployment)
    job_id, result = await _run_probe(fleet_engine, execution, {"probe": "raise"}, max_attempts=1)
    assert result is None
    job = await job_status(fleet_engine, job_id)
    assert job is not None and job.state == "dead_lettered"
    assert job.last_error == "RuntimeError: the probe failed underneath the task"


async def test_cancelling_a_running_task_stops_its_container(
    fleet_engine: AsyncEngine, docker: Docker, task_image: str, deployment: str
) -> None:
    execution = _execution(docker, task_image, deployment)
    await execution.ready()
    submission = await submit_batch(
        fleet_engine,
        label="b",
        jobs=[NewJob(name="sleep", payload={"probe": "sleep", "seconds": 60})],
    )
    job_id = submission.job_ids[0]
    worker = Worker(fleet_engine, execution, worker_id="w", lease_seconds=30, heartbeat_seconds=0.2)
    stepping = asyncio.create_task(worker.step())
    deadline = time.monotonic() + 30
    while not await _containers(docker, deployment):
        assert time.monotonic() < deadline, "the container never started"
        await asyncio.sleep(0.1)
    requested = time.monotonic()
    assert await cancel(fleet_engine, job_id) == "running"
    await asyncio.wait_for(stepping, timeout=30)
    stopped_after = time.monotonic() - requested
    job = await job_status(fleet_engine, job_id)
    assert job is not None and (job.state, job.lease_expires_at) == ("cancelled", None)
    assert worker.cancelled == 1
    assert await _containers(docker, deployment) == []
    # stopped within a heartbeat or so, not when the 30 s lease would have run out
    assert stopped_after < 10, stopped_after


async def test_containers_of_attempts_that_lost_their_lease_are_stopped(
    fleet_engine: AsyncEngine, docker: Docker, task_image: str, deployment: str
) -> None:
    submission = await submit_batch(fleet_engine, label="b", jobs=[NewJob(name="j", payload={})])
    job_id = submission.job_ids[0]
    dead = await claim(fleet_engine, worker_id="dead", lease_seconds=60)
    assert dead is not None
    config = task_config(
        image=task_image,
        runner=PROBES,
        policy=ExecutionPolicy(),
        input_dir=Path("/nonexistent/in"),
        output_dir=Path("/nonexistent/out"),
        network=None,
        proxy=None,
        labels=labels(job_id=job_id, attempt=1, worker_id="dead") | {DEPLOYMENT: deployment},
    )
    config["Cmd"] = ["sleep", "300"]
    config["HostConfig"]["Binds"] = []
    orphan = await docker.create(f"fleet-{deployment}-orphan", config)
    await docker.start(orphan)
    execution = _execution(docker, task_image, deployment)
    try:
        # while its attempt still holds a lease, the container is left alone
        assert await execution.reap_orphans(lambda: live_attempts(fleet_engine)) == []
        async with fleet_engine.begin() as connection:
            await connection.execute(
                text("update fleet_jobs set lease_expires_at = clock_timestamp() where id = :job"),
                {"job": job_id},
            )
        assert await execution.reap_orphans(lambda: live_attempts(fleet_engine)) == [orphan]
        assert await _containers(docker, deployment) == []
    finally:
        with contextlib.suppress(Exception):
            await docker.remove(orphan)
