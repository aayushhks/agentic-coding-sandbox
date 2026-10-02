"""Run a bench batch through the fleet: an api process, worker processes, Postgres between."""

import asyncio
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Literal

from sqlalchemy.ext.asyncio import create_async_engine

from bench.executor import BatchResult, TaskExecution, job_result
from bench.jobs import AttemptRun, FailureKind, JobResult, Outcome
from bench.replay import Recording
from bench.resources import Sampler, combine_calls, named
from bench.runner import execution_from_body, recording_from_body
from bench.taskset import BenchTask, Job, TaskSet
from fleet.client import FleetClient
from fleet.models import NewJob
from fleet.policy import DEFAULT_POLICY, ExecutionPolicy
from fleet.store import attempt_history, batch_jobs, executions
from fleet.store import job_result as published_result

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
PayloadFactory = Callable[[BenchTask], dict[str, Any]]

# a job whose every attempt failed underneath the task: what the attempts spent went with them,
# and its attempt history says how each one ended
DEAD_LETTERED = TaskExecution(
    outcome=Outcome.FAILED,
    failure_mode="dead_lettered",
    failure_kind=FailureKind.INFRA,
    matched_expectation=False,
    termination_reason="dead_lettered",
    iterations=0,
    llm_calls=0,
    prompt_tokens=0,
    completion_tokens=0,
    retry_wait_seconds=0.0,
    divergence=None,
)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def spawn(args: list[str], env: dict[str, str], log: IO[bytes]) -> "subprocess.Popen[bytes]":
    return subprocess.Popen(
        [sys.executable, *args],
        cwd=_BACKEND_ROOT,
        env={**os.environ, "PYTHONPATH": str(_BACKEND_ROOT), **env},
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def tail(log: IO[bytes]) -> str:
    log.seek(0)
    return log.read().decode(errors="replace")[-2000:]


def ready_workers(folder: Path) -> int:
    return len(list(folder.glob("w*.ready")))


def worker_reports(folder: Path) -> list[dict[str, Any]]:
    """What each worker wrote as it exited; one that had to be killed wrote nothing."""
    return [json.loads(path.read_text()) for path in sorted(folder.glob("w*.json"))]


def pool_topology(workers: int, database: str = "Postgres", *, containers: bool = False) -> str:
    processes = "one fleet worker process" if workers == 1 else f"{workers} fleet worker processes"
    where = "; each job in a container of its own" if containers else ""
    return f"single host: {processes}, the fleet api and {database}, all on this machine{where}"


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
        mode: Literal["process", "container"] = "process",
        task_image: str = "fleet-task:local",
        image_id: str | None = None,
        policy: ExecutionPolicy = DEFAULT_POLICY,
        postgres_pid: int | None = None,
        runner: str = "bench.runner:run_job",
    ) -> None:
        if workers < 1:
            raise ValueError("the fleet needs at least one worker")
        self.workers = workers
        self.mode = mode
        self.topology = topology or pool_topology(workers, containers=mode == "container")
        # what the run record says about where jobs ran and under what limits
        self.execution: dict[str, Any] | None = {
            "mode": mode,
            "policy": policy.model_dump(mode="json"),
        } | ({"image": task_image, "image_id": image_id} if mode == "container" else {})
        self._url = database_url
        # unset, the workers take the fleet's configured lease
        self._lease = lease_seconds
        self._image = task_image
        self._policy = policy
        # its own label, so its workers only ever reap the containers this run started
        self._deployment = f"bench-{uuid.uuid4().hex[:8]}"
        # the database server's main process, when it runs on this host where its cpu can be read
        self._postgres_pid = postgres_pid
        self._runner = runner

    async def run(
        self, taskset: TaskSet, jobs: Sequence[Job], payload_for: PayloadFactory
    ) -> BatchResult:
        port = free_port()
        env = {"FLEET_DATABASE_URL": self._url}
        options = [] if self._lease is None else ["--lease-seconds", str(self._lease)]
        if self.mode == "container":
            options += ["--execution", "container", "--task-image", self._image]
            options += ["--deployment", self._deployment]
        with contextlib.ExitStack() as logs:
            api_log = logs.enter_context(tempfile.TemporaryFile())
            worker_logs = [
                logs.enter_context(tempfile.TemporaryFile()) for _ in range(self.workers)
            ]
            stats = Path(logs.enter_context(tempfile.TemporaryDirectory()))
            # no access log: the status polling below would otherwise write a line per request
            api = spawn(
                [
                    *("-m", "uvicorn", "fleet.api:app", "--host", "127.0.0.1"),
                    *("--port", str(port), "--log-level", "warning", "--no-access-log"),
                ],
                env,
                api_log,
            )
            # the workers start before the submit and are waited for, so no job pays for a start
            workers = [
                spawn(
                    [
                        *("-m", "fleet.worker", "--runner", self._runner),
                        *("--worker-id", f"w{index}", "--database-url", self._url, *options),
                        *("--ready-file", str(stats / f"w{index}.ready")),
                        *("--stats-out", str(stats / f"w{index}.json")),
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
                        raise RuntimeError(f"the fleet api never came up:\n{tail(api_log)}")
                    await asyncio.sleep(0.1)
                while ready_workers(stats) < self.workers:
                    for worker, log in zip(workers, worker_logs, strict=True):
                        if worker.poll() is not None:
                            raise RuntimeError(f"a fleet worker exited early:\n{tail(log)}")
                    if time.monotonic() > deadline:
                        raise RuntimeError("the fleet workers never came up")
                    await asyncio.sleep(0.05)
                groups = {
                    "workers": [worker.pid for worker in workers],
                    "api": [api.pid],
                    "postgres": [] if self._postgres_pid is None else [self._postgres_pid],
                    "docker": named(["dockerd", "containerd"]),
                }
                # from just before the submit until the batch is seen to be done
                async with Sampler(groups) as sampler:
                    submission = await client.submit(
                        label="bench",
                        jobs=[
                            NewJob(name=job.id, payload=payload_for(taskset.get(job.task_id)))
                            for job in jobs
                        ],
                        idempotency_key=uuid.uuid4().hex,
                        policy=self._policy,
                    )
                    while not (await client.batch(submission.batch_id)).done:
                        for worker, log in zip(workers, worker_logs, strict=True):
                            if worker.poll() is not None:
                                raise RuntimeError(f"a fleet worker exited early:\n{tail(log)}")
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
            reports = worker_reports(stats)
            database = combine_calls(reports) if len(reports) == self.workers else None
        results, recordings = await self._results(taskset, jobs, submission.batch_id)
        return BatchResult(
            results,
            None,
            resources=sampler.result,
            database=database,
            recordings=recordings,
        )

    async def _results(
        self, taskset: TaskSet, jobs: Sequence[Job], batch_id: int
    ) -> tuple[list[JobResult], list[Recording]]:
        """Rebuild bench results from what the fleet stored, timed by Postgres's clock, and the
        responses any real job got."""
        engine = create_async_engine(self._url)
        try:
            rows = await batch_jobs(engine, batch_id)
            published = {row.id: await published_result(engine, row.id) for row in rows}
            ran = await executions(engine, batch_id)
            history = await attempt_history(engine, batch_id)
        finally:
            await engine.dispose()
        by_name = {job.id: job for job in jobs}
        start = min(row.submitted_at for row in rows)

        def since(moment: datetime | None) -> float | None:
            return None if moment is None else (moment - start).total_seconds()

        results = []
        recordings = []
        for row in rows:
            result = published[row.id]
            dead = row.state == "dead_lettered"
            if (result is None and not dead) or row.claimed_at is None or row.finished_at is None:
                raise RuntimeError(f"job {row.name} finished without a complete record")
            job = by_name[row.name]
            if result is None:
                execution = DEAD_LETTERED
            else:
                execution = execution_from_body(result.body)
                recording = recording_from_body(result.body)
                if recording is not None:
                    recordings.append(recording)
            results.append(
                job_result(
                    job,
                    taskset.get(job.task_id),
                    execution,
                    worker=row.worker_id or "",
                    attempts=row.attempt,
                    submitted_at=(row.submitted_at - start).total_seconds(),
                    claimed_at=(row.claimed_at - start).total_seconds(),
                    finished_at=(row.finished_at - start).total_seconds(),
                    ran=ran.get(row.id),
                    history=[
                        AttemptRun(
                            attempt=attempt.attempt,
                            worker=attempt.worker_id,
                            claimed_at=(attempt.claimed_at - start).total_seconds(),
                            ended_at=since(attempt.ended_at),
                            ended_by=attempt.ended_by,
                            error=attempt.error,
                        )
                        for attempt in history.get(row.id, [])
                    ],
                )
            )
        return results, recordings
