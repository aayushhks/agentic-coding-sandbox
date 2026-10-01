"""How long a cancel takes, through the fleet's api and one worker on the fleet's own lease.

Each round submits a job that would sleep far past the round and waits for it to start. A second
job, submitted then, queues behind it and is cancelled at once, in the cancel's own transaction.
The running job is cancelled at a seeded random point between 5% and 95% of its first heartbeat
interval. Its worker notices at that heartbeat, stops the run (in a container: kills and removes
it) and ends the job, releasing the lease. The times come from Postgres's clock.

    uv run python -m bench.cancellation --execution container --rounds 20 --out cancel.json
"""

import argparse
import asyncio
import contextlib
import json
import random
import signal
import statistics
import subprocess
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bench.environment import capture_environment
from bench.fleet_executor import free_port, spawn, tail
from fleet.client import FleetClient
from fleet.config import FleetSettings, async_url
from fleet.containers import MANAGED
from fleet.docker import Docker
from fleet.execution import DEPLOYMENT
from fleet.invariants import check, snapshot
from fleet.localdb import LocalPostgres
from fleet.migrate import migrate
from fleet.models import JobStatus, NewJob
from fleet.store import job_status, server_version

# far past any round, so only the cancel can end it
SLEEP = {"probe": "sleep", "seconds": 3600}


async def _until(engine: AsyncEngine, job_id: int, state: str, seconds: float = 120) -> JobStatus:
    deadline = time.monotonic() + seconds
    while (status := await job_status(engine, job_id)) is None or status.state != state:
        if time.monotonic() > deadline:
            raise RuntimeError(f"job {job_id} never got to {state}")
        await asyncio.sleep(0.02)
    return status


async def _submit(client: FleetClient, name: str) -> int:
    submission = await client.submit(
        label="cancel", jobs=[NewJob(name=name, payload=SLEEP)], idempotency_key=uuid.uuid4().hex
    )
    return submission.job_ids[0]


async def _round(
    client: FleetClient, engine: AsyncEngine, heartbeat: float, at: float
) -> dict[str, Any]:
    running = await _submit(client, "running")
    started = await _until(engine, running, "running")
    # the one worker is busy, so this one can only queue
    queued = await _submit(client, "queued")
    asked = time.monotonic()
    answer = await client.cancel(queued)
    queued_seconds = time.monotonic() - asked
    assert started.started_at is not None
    # Postgres stamps on this machine's clock, so its timestamps and ours line up
    into = (datetime.now(UTC) - started.started_at).total_seconds()
    await asyncio.sleep(max(0.0, at * heartbeat - into))
    if (await client.cancel(running)).state != "running":
        raise RuntimeError(f"job {running} was no longer running when cancelled")
    ended = await _until(engine, running, "cancelled")
    gone = await job_status(engine, queued)
    assert ended.cancel_requested_at is not None and ended.finished_at is not None
    requested = (ended.cancel_requested_at - started.started_at).total_seconds()
    latency = (ended.finished_at - ended.cancel_requested_at).total_seconds()
    return {
        "job_ids": [running, queued],
        "queued": {
            "answer": answer.state,
            "attempts": None if gone is None else gone.attempt,
            "api_round_trip_seconds": round(queued_seconds, 4),
        },
        "running": {
            "attempts": ended.attempt,
            "lease_released": ended.lease_expires_at is None,
            "requested_into_run_seconds": round(requested, 4),
            "latency_seconds": round(latency, 4),
            # the first heartbeat comes one interval after the run started
            "until_heartbeat_seconds": round(heartbeat - requested, 4),
            "stop_seconds": round(latency - (heartbeat - requested), 4),
        },
    }


def _spread(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "median": round(statistics.median(ordered), 4),
        "p95": round(ordered[max(0, round(0.95 * len(ordered)) - 1)], 4),
        "min": round(ordered[0], 4),
        "max": round(ordered[-1], 4),
    }


async def _measure(url: str, args: argparse.Namespace) -> dict[str, Any]:
    settings = FleetSettings()
    lease = settings.lease_seconds
    heartbeat = settings.heartbeat_seconds or lease / 3
    deployment = f"cancel-{uuid.uuid4().hex[:8]}"
    options = ["--lease-seconds", str(lease)]
    image_id = None
    docker = Docker(settings.docker_socket)
    if args.execution == "container":
        image_id = await docker.image_id(settings.task_image)
        if image_id is None:
            raise RuntimeError(f"no {settings.task_image} image; build it first")
        options += ["--execution", "container", "--task-image", settings.task_image]
        options += ["--deployment", deployment]
    engine = create_async_engine(async_url(url))
    port = free_port()
    env = {"FLEET_DATABASE_URL": url}
    rng = random.Random(args.seed)
    rounds: list[dict[str, Any]] = []
    with contextlib.ExitStack() as logs:
        api_log = logs.enter_context(tempfile.TemporaryFile())
        worker_log = logs.enter_context(tempfile.TemporaryFile())
        api = spawn(
            [
                *("-m", "uvicorn", "fleet.api:app", "--host", "127.0.0.1", "--port", str(port)),
                *("--log-level", "warning", "--no-access-log"),
            ],
            env,
            api_log,
        )
        worker = spawn(
            [
                *("-m", "fleet.worker", "--runner", "fleet.probes:run", "--worker-id", "w0"),
                *("--database-url", url, *options),
            ],
            env,
            worker_log,
        )
        client = FleetClient(f"http://127.0.0.1:{port}")
        try:
            deadline = time.monotonic() + 60
            while not await client.healthy():
                if api.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"the fleet api never came up:\n{tail(api_log)}")
                await asyncio.sleep(0.1)
            for number in range(args.rounds):
                if worker.poll() is not None:
                    raise RuntimeError(f"the worker exited:\n{tail(worker_log)}")
                rounds.append(await _round(client, engine, heartbeat, rng.uniform(0.05, 0.95)))
                print(f"round {number + 1}/{args.rounds}: {rounds[-1]['running']}", flush=True)
            submitted = [job for item in rounds for job in item["job_ids"]]
            violations = check(await snapshot(engine, submitted), submitted)
            left = await docker.containers({MANAGED: "1", DEPLOYMENT: deployment})
            version = await server_version(engine)
        finally:
            await client.aclose()
            await docker.aclose()
            await engine.dispose()
            for process in (worker, api):
                process.send_signal(signal.SIGTERM)
            for process in (worker, api):
                try:
                    await asyncio.to_thread(process.wait, 30)
                except subprocess.TimeoutExpired:
                    process.kill()
    running = [item["running"] for item in rounds]
    queued = [item["queued"] for item in rounds]
    return {
        "config": {
            "execution": args.execution,
            "rounds": args.rounds,
            "seed": args.seed,
            "lease_seconds": lease,
            "heartbeat_seconds": heartbeat,
            "cancel_point": "uniform over 5-95% of the first heartbeat interval",
            "image": settings.task_image if image_id else None,
            "image_id": image_id,
            "database": version,
            "topology": "single host: the fleet api, one worker process and Postgres",
        },
        "environment": capture_environment().model_dump(mode="json"),
        "checks": {
            "invariant_violations": [str(violation) for violation in violations],
            "containers_left": len(left),
            "running_jobs_cancelled_on_first_attempt": all(
                item["attempts"] == 1 and item["lease_released"] for item in running
            ),
            "queued_jobs_never_claimed": all(
                item["answer"] == "cancelled" and item["attempts"] == 0 for item in queued
            ),
        },
        "summary": {
            "running_latency_seconds": _spread([item["latency_seconds"] for item in running]),
            "running_stop_seconds": _spread([item["stop_seconds"] for item in running]),
            "queued_api_round_trip_seconds": _spread(
                [item["api_round_trip_seconds"] for item in queued]
            ),
        },
        "rounds": rounds,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution", choices=["process", "container"], default="container")
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--database-url", default=None, help="default: a throwaway local Postgres")
    args = parser.parse_args(argv)
    cluster = None if args.database_url else LocalPostgres.start()
    url: str = args.database_url or (cluster.url if cluster else "")
    try:
        migrate(url)
        record = asyncio.run(_measure(url, args))
    finally:
        if cluster is not None:
            cluster.stop()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"checks": record["checks"], "summary": record["summary"]}, indent=2))
    checks = record["checks"]
    passed = (
        not checks["invariant_violations"]
        and checks["containers_left"] == 0
        and checks["running_jobs_cancelled_on_first_attempt"]
        and checks["queued_jobs_never_claimed"]
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
