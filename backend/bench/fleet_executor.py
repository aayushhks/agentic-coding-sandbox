"""Run a bench batch through the fleet: an api process, worker processes, Postgres between."""

import asyncio
import contextlib
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO, Any

from sqlalchemy.ext.asyncio import create_async_engine

from bench.executor import BatchResult, job_result
from bench.jobs import JobResult
from bench.runner import execution_from_body
from bench.taskset import BenchTask, Job, TaskSet
from fleet.client import FleetClient
from fleet.models import NewJob
from fleet.store import batch_jobs
from fleet.store import job_result as published_result

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
PayloadFactory = Callable[[BenchTask], dict[str, Any]]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def _spawn(args: list[str], env: dict[str, str], log: IO[bytes]) -> "subprocess.Popen[bytes]":
    return subprocess.Popen(
        [sys.executable, *args],
        cwd=_BACKEND_ROOT,
        env={**os.environ, "PYTHONPATH": str(_BACKEND_ROOT), **env},
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def _tail(log: IO[bytes]) -> str:
    log.seek(0)
    return log.read().decode(errors="replace")[-2000:]


def pool_topology(workers: int, database: str = "Postgres") -> str:
    processes = "one fleet worker process" if workers == 1 else f"{workers} fleet worker processes"
    return f"single host: {processes}, the fleet api and {database}, all on this machine"


class FleetExecutor:
    """Submits through the fleet api; a pool of worker processes claims, runs and publishes."""

    name = "fleet"

    def __init__(
        self,
        database_url: str,
        *,
        workers: int = 1,
        lease_seconds: float | None = None,
        topology: str | None = None,
    ) -> None:
        if workers < 1:
            raise ValueError("the fleet needs at least one worker")
        self.workers = workers
        self.topology = topology or pool_topology(workers)
        self._url = database_url
        # unset, the workers take the fleet's configured lease
        self._lease = lease_seconds

    async def run(
        self, taskset: TaskSet, jobs: Sequence[Job], payload_for: PayloadFactory
    ) -> BatchResult:
        port = _free_port()
        env = {"FLEET_DATABASE_URL": self._url}
        lease = [] if self._lease is None else ["--lease-seconds", str(self._lease)]
        with contextlib.ExitStack() as logs:
            api_log = logs.enter_context(tempfile.TemporaryFile())
            worker_logs = [
                logs.enter_context(tempfile.TemporaryFile()) for _ in range(self.workers)
            ]
            # no access log: the status polling below would otherwise write a line per request
            api = _spawn(
                [
                    *("-m", "uvicorn", "fleet.api:app", "--host", "127.0.0.1"),
                    *("--port", str(port), "--log-level", "warning", "--no-access-log"),
                ],
                env,
                api_log,
            )
            # the workers start before the submit, so their start-up isn't charged to any job
            workers = [
                _spawn(
                    [
                        *("-m", "fleet.worker", "--runner", "bench.runner:run_job"),
                        *("--worker-id", f"w{index}", "--database-url", self._url, *lease),
                    ],
                    env,
                    log,
                )
                for index, log in enumerate(worker_logs)
            ]
            client = FleetClient(f"http://127.0.0.1:{port}")
            try:
                deadline = time.monotonic() + 60
                while not await client.healthy():
                    if api.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError(f"the fleet api never came up:\n{_tail(api_log)}")
                    await asyncio.sleep(0.1)
                submission = await client.submit(
                    label="bench",
                    jobs=[
                        NewJob(name=job.id, payload=payload_for(taskset.get(job.task_id)))
                        for job in jobs
                    ],
                    idempotency_key=uuid.uuid4().hex,
                )
                while not (await client.batch(submission.batch_id)).done:
                    for worker, log in zip(workers, worker_logs, strict=True):
                        if worker.poll() is not None:
                            raise RuntimeError(f"a fleet worker exited early:\n{_tail(log)}")
                    await asyncio.sleep(0.1)
            finally:
                await client.aclose()
                for process in (*workers, api):
                    process.send_signal(signal.SIGTERM)
                for process in (*workers, api):
                    try:
                        await asyncio.to_thread(process.wait, 30)
                    except subprocess.TimeoutExpired:
                        process.kill()
        return BatchResult(await self._results(taskset, jobs, submission.batch_id), None)

    async def _results(
        self, taskset: TaskSet, jobs: Sequence[Job], batch_id: int
    ) -> list[JobResult]:
        """Rebuild bench results from what the fleet stored, timed by Postgres's clock."""
        engine = create_async_engine(self._url)
        try:
            rows = await batch_jobs(engine, batch_id)
            published = {row.id: await published_result(engine, row.id) for row in rows}
        finally:
            await engine.dispose()
        by_name = {job.id: job for job in jobs}
        start = min(row.submitted_at for row in rows)
        results = []
        for row in rows:
            result = published[row.id]
            if row.state == "dead_lettered":
                # a bench record needs the task's execution, and a dead letter never produced one
                raise RuntimeError(
                    f"job {row.name} was dead-lettered after {row.attempt} attempts: "
                    f"{row.last_error}"
                )
            if result is None or row.claimed_at is None or row.finished_at is None:
                raise RuntimeError(f"job {row.name} finished without a complete record")
            job = by_name[row.name]
            results.append(
                job_result(
                    job,
                    taskset.get(job.task_id),
                    execution_from_body(result.body),
                    worker=row.worker_id or "",
                    attempts=row.attempt,
                    submitted_at=(row.submitted_at - start).total_seconds(),
                    claimed_at=(row.claimed_at - start).total_seconds(),
                    finished_at=(row.finished_at - start).total_seconds(),
                )
            )
        return results
