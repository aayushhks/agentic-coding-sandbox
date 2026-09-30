"""Run a bench batch through the fleet: an api process, a worker process, Postgres between."""

import asyncio
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


class FleetExecutor:
    """Submits through the fleet api; one worker process claims, runs and publishes every job."""

    name = "fleet"

    def __init__(
        self,
        database_url: str,
        *,
        lease_seconds: float = 600.0,
        topology: str = "single host: one fleet worker process, the fleet api and Postgres",
    ) -> None:
        # one worker until heartbeats and fencing make several safe
        self.workers = 1
        self.topology = topology
        self._url = database_url
        self._lease = lease_seconds

    async def run(
        self, taskset: TaskSet, jobs: Sequence[Job], payload_for: PayloadFactory
    ) -> BatchResult:
        port = _free_port()
        env = {"FLEET_DATABASE_URL": self._url}
        with tempfile.TemporaryFile() as api_log, tempfile.TemporaryFile() as worker_log:
            # no access log: the status polling below would otherwise write a line per request
            api = _spawn(
                [
                    *("-m", "uvicorn", "fleet.api:app", "--host", "127.0.0.1"),
                    *("--port", str(port), "--log-level", "warning", "--no-access-log"),
                ],
                env,
                api_log,
            )
            # the worker starts before the submit, so its start-up isn't charged to any job
            worker = _spawn(
                [
                    *(
                        "-m",
                        "fleet.worker",
                        "--runner",
                        "bench.runner:run_job",
                        "--worker-id",
                        "w0",
                    ),
                    *("--lease-seconds", str(self._lease), "--database-url", self._url),
                ],
                env,
                worker_log,
            )
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
                    if worker.poll() is not None:
                        raise RuntimeError(f"the fleet worker exited early:\n{_tail(worker_log)}")
                    await asyncio.sleep(0.1)
            finally:
                await client.aclose()
                for process in (worker, api):
                    process.send_signal(signal.SIGTERM)
                for process in (worker, api):
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
