"""Where a container's extra time per job goes, measured on the same jobs in process and in one.

Each round runs two probe jobs through a worker, once in process and once in containers, in
alternating order: a job that does nothing, which leaves only the cost of running a job at all,
and a job that times what every agent task pays wherever it runs (starting python, importing the
agent's runner, and sandboxed commands). Containers get the default policy.

    uv run python -m bench.container_overhead --rounds 10 --out overhead.json
"""

import argparse
import asyncio
import json
import statistics
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bench.environment import capture_environment
from fleet.config import FleetSettings, async_url
from fleet.docker import Docker
from fleet.execution import Execution, InContainer, InProcess
from fleet.localdb import LocalPostgres
from fleet.migrate import migrate
from fleet.models import NewJob
from fleet.policy import DEFAULT_POLICY
from fleet.runners import load_runner
from fleet.store import executions, job_result, job_status, server_version, submit_batch
from fleet.worker import Worker

PROBES = "fleet.probes:run"
COMMANDS = {"sandbox_true": "true", "sandbox_pytest_version": "python -m pytest --version"}


async def _round(engine: AsyncEngine, execution: Execution, repeat: int) -> dict[str, Any]:
    timing = {"probe": "overhead", "module": "bench.runner", "commands": COMMANDS, "repeat": repeat}
    submission = await submit_batch(
        engine,
        label="overhead",
        jobs=[
            NewJob(name="nothing", payload={"probe": "sleep", "seconds": 0}),
            NewJob(name="timing", payload=timing),
        ],
    )
    worker = Worker(engine, execution, worker_id="w0", lease_seconds=FleetSettings().lease_seconds)
    for _ in submission.job_ids:
        await worker.step()
    ran = await executions(engine, submission.batch_id)
    measured: dict[str, Any] = {}
    for job_id in submission.job_ids:
        status, result = await job_status(engine, job_id), await job_result(engine, job_id)
        if status is None or result is None or result.outcome != "succeeded":
            raise RuntimeError(f"probe job {job_id} did not succeed: {status}, {result}")
        assert status.started_at is not None and status.finished_at is not None
        record = ran.get(job_id)
        measured[status.name] = {
            # from marking the job running to its result landing, on Postgres's clock
            "service_seconds": round((status.finished_at - status.started_at).total_seconds(), 4),
            "container_seconds": None if record is None else record["seconds"],
            "body": result.body,
        }
    return measured


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 4)


def _summary(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    nothing = [item["nothing"] for item in rounds]
    timing = [item["timing"]["body"] for item in rounds]
    summary: dict[str, Any] = {
        "nothing_service_seconds": _median([item["service_seconds"] for item in nothing]),
    }
    if nothing[0]["container_seconds"] is not None:
        inside = [item["container_seconds"] for item in nothing]
        summary["nothing_container_seconds"] = _median(inside)
        summary["nothing_outside_container_seconds"] = _median(
            [item["service_seconds"] - item["container_seconds"] for item in nothing]
        )
    for name in ("python_startup", "import", *COMMANDS):
        summary[f"{name}_seconds"] = _median([value for body in timing for value in body[name]])
    return summary


async def _measure(url: str, args: argparse.Namespace) -> dict[str, Any]:
    settings = FleetSettings()
    docker = Docker(settings.docker_socket)
    image_id = await docker.image_id(settings.task_image)
    if image_id is None:
        raise RuntimeError(f"no {settings.task_image} image; build it first")
    engine = create_async_engine(async_url(url))
    modes: dict[str, Execution] = {
        "process": InProcess(load_runner(PROBES)),
        "container": InContainer(
            docker,
            image=settings.task_image,
            runner=PROBES,
            deployment=f"overhead-{uuid.uuid4().hex[:8]}",
        ),
    }
    rounds: dict[str, list[dict[str, Any]]] = {name: [] for name in modes}
    try:
        await modes["container"].ready()
        for number in range(args.rounds):
            # alternating which goes first, so neither always runs on a warmer machine
            order = list(modes) if number % 2 == 0 else list(reversed(modes))
            for name in order:
                rounds[name].append(await _round(engine, modes[name], args.repeat))
            print(f"round {number + 1}/{args.rounds} done", flush=True)
        version = await server_version(engine)
    finally:
        await docker.aclose()
        await engine.dispose()
    return {
        "config": {
            "rounds": args.rounds,
            "repeat_per_round": args.repeat,
            "module": "bench.runner",
            "commands": COMMANDS,
            "policy": DEFAULT_POLICY.model_dump(mode="json"),
            "image": settings.task_image,
            "image_id": image_id,
            "database": version,
            "topology": "single host: one worker in this process, Postgres on the same machine",
        },
        "environment": capture_environment().model_dump(mode="json"),
        "summary": {name: _summary(items) for name, items in rounds.items()},
        "rounds": rounds,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--repeat", type=int, default=10, help="timings of each kind per round")
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
    print(json.dumps(record["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
