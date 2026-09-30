"""A fleet worker: claim a job, run it in-process or in a container, publish the result, repeat."""

import argparse
import asyncio
import logging
import os
import signal
import socket
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from fleet.config import FleetSettings, async_url
from fleet.docker import Docker
from fleet.execution import Attempted, Execution, InContainer, InProcess
from fleet.models import DEFAULT_RETRY, ClaimedJob, RetryPolicy
from fleet.runners import Runner, RunnerError, load_runner
from fleet.store import (
    claim,
    finish_cancelled,
    heartbeat,
    job_status,
    live_attempts,
    publish,
    reap,
    record_execution,
    release,
    start,
    unfinished_jobs,
)

logger = logging.getLogger(__name__)


Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class Cancelled:
    """A heartbeat found the job's cancel requested, so the run was stopped."""


class Worker:
    def __init__(
        self,
        engine: AsyncEngine,
        run: Runner | Execution,
        *,
        worker_id: str,
        lease_seconds: float,
        heartbeat_seconds: float | None = None,
        reap_every_seconds: float = 5.0,
        retry: RetryPolicy = DEFAULT_RETRY,
        min_poll_seconds: float = 0.05,
        max_poll_seconds: float = 0.5,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        beat = lease_seconds / 3 if heartbeat_seconds is None else heartbeat_seconds
        if not 0 < beat < lease_seconds:
            raise ValueError("a heartbeat has to come sooner than the lease runs out")
        self.worker_id = worker_id
        self._engine = engine
        # a plain runner runs in this process; containers come in through an execution
        self._execution = run if isinstance(run, Execution) else InProcess(run)
        self._lease = lease_seconds
        self._heartbeat = beat
        self._reap_every = reap_every_seconds
        self._retry = retry
        self._min_poll = min_poll_seconds
        self._max_poll = max_poll_seconds
        self._sleep = sleep
        self._clock = clock
        self._reaped_at = float("-inf")
        self.published = 0
        # results refused because the lease had run out by the time the job finished
        self.rejected = 0
        # attempts that lost their lease before they could finish
        self.lost = 0
        # attempts given back for a retry after an infrastructure failure
        self.released = 0
        # jobs stopped and ended because their cancel was requested
        self.cancelled = 0

    async def _reap(self, *, orphans: bool = False) -> None:
        await reap(self._engine, retry=self._retry)
        if orphans:
            self._reaped_at = self._clock()
            await self._execution.reap_orphans(lambda: live_attempts(self._engine))

    async def _run(self, job: ClaimedJob) -> Attempted | Cancelled | None:
        """Run a job while extending its lease; None when the lease was lost and the run stopped."""
        work = asyncio.ensure_future(self._execution.run(job, self.worker_id))
        try:
            while True:
                done, _ = await asyncio.wait({work}, timeout=self._heartbeat)
                if done:
                    return work.result()
                beat = await heartbeat(
                    self._engine, job_id=job.id, attempt=job.attempt, lease_seconds=self._lease
                )
                if beat is None:
                    return None
                if beat.cancel_requested:
                    return Cancelled()
        finally:
            # whatever ends the wait, the run never outlives it
            work.cancel()
            await asyncio.wait({work})

    async def step(self) -> bool:
        """Run one job; False when there was nothing to claim, even after reaping lapsed leases."""
        # reaping on a timer keeps it off the per-job path while busy
        if self._clock() - self._reaped_at >= self._reap_every:
            await self._reap(orphans=True)
        job = await claim(self._engine, worker_id=self.worker_id, lease_seconds=self._lease)
        if job is None:
            await self._reap()
            job = await claim(self._engine, worker_id=self.worker_id, lease_seconds=self._lease)
        if job is None:
            return False
        if not await start(self._engine, job_id=job.id, attempt=job.attempt):
            # cancelled before it started, or its lease lapsed and it is no longer this worker's
            if not await self._cancel(job):
                self.lost += 1
                logger.info(
                    "%s: lost the lease on job %s before starting it", self.worker_id, job.id
                )
            return True
        outcome = await self._run(job)
        if isinstance(outcome, Cancelled):
            if not await self._cancel(job):
                self.lost += 1
            return True
        if outcome is None:
            self.lost += 1
            logger.info("%s: lost the lease on job %s, run stopped", self.worker_id, job.id)
            return True
        if outcome.execution is not None:
            await record_execution(
                self._engine,
                job_id=job.id,
                attempt=job.attempt,
                execution=outcome.execution,
                logs=outcome.logs,
            )
        if isinstance(outcome.result, RunnerError):
            await self._release(job, outcome.result.error)
            return True
        accepted = await publish(
            self._engine,
            job_id=job.id,
            attempt=job.attempt,
            worker_id=self.worker_id,
            outcome=outcome.result.outcome,
            body=outcome.result.body,
        )
        if accepted:
            self.published += 1
        elif (status := await job_status(self._engine, job.id)) and status.state == "cancelled":
            # a cancel requested before the result arrived ended the job instead
            self.cancelled += 1
            logger.info("%s: job %s was cancelled before its result landed", self.worker_id, job.id)
        else:
            self.rejected += 1
            logger.info(
                "%s: result for job %s attempt %s refused, its lease ran out",
                self.worker_id,
                job.id,
                job.attempt,
            )
        return True

    async def _cancel(self, job: ClaimedJob) -> bool:
        """End a job whose cancel was requested, releasing its lease at once."""
        if not await finish_cancelled(self._engine, job_id=job.id, attempt=job.attempt):
            return False
        self.cancelled += 1
        logger.info("%s: job %s cancelled, lease released", self.worker_id, job.id)
        return True

    async def _release(self, job: ClaimedJob, error: str) -> None:
        state = await release(
            self._engine, job_id=job.id, attempt=job.attempt, error=error, retry=self._retry
        )
        if state is None:
            # too late to give it back: the lease ran out, and a reaper ends the attempt instead
            self.lost += 1
            logger.info("%s: lost the lease on job %s after: %s", self.worker_id, job.id, error)
            return
        self.released += 1
        logger.info(
            "%s: job %s attempt %s failed underneath the task, now %s: %s",
            self.worker_id,
            job.id,
            job.attempt,
            state,
            error,
        )

    async def run(self, *, exit_when_idle: bool = False, stop: asyncio.Event | None = None) -> int:
        """Work until stopped; with exit_when_idle, until no job anywhere is unfinished."""
        delay = self._min_poll
        while stop is None or not stop.is_set():
            if await self.step():
                delay = self._min_poll
                continue
            if exit_when_idle and await unfinished_jobs(self._engine) == 0:
                break
            await self._sleep(delay)
            delay = min(delay * 2, self._max_poll)
        return self.published


async def serve(
    *,
    url: str,
    run: Runner | Execution,
    worker_id: str,
    lease_seconds: float,
    heartbeat_seconds: float | None,
    reap_every_seconds: float,
    retry: RetryPolicy,
    exit_when_idle: bool,
) -> Worker:
    engine = create_async_engine(async_url(url))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # a terminate request lets the current job finish and publish before the worker exits
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    execution = run if isinstance(run, Execution) else InProcess(run)
    await execution.ready()
    worker = Worker(
        engine,
        execution,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds,
        reap_every_seconds=reap_every_seconds,
        retry=retry,
    )
    try:
        await worker.run(exit_when_idle=exit_when_idle, stop=stop)
    finally:
        await execution.close()
        await engine.dispose()
    return worker


def main(argv: list[str] | None = None) -> int:
    settings = FleetSettings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", required=True, help="the job runner, as module:function")
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}-{os.getpid()}")
    parser.add_argument("--lease-seconds", type=float, default=settings.lease_seconds)
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=settings.heartbeat_seconds,
        help="default: a third of the lease",
    )
    parser.add_argument("--reap-every-seconds", type=float, default=settings.reap_every_seconds)
    parser.add_argument(
        "--retry-backoff-seconds", type=float, default=settings.retry_backoff_seconds
    )
    parser.add_argument(
        "--retry-backoff-cap-seconds", type=float, default=settings.retry_backoff_cap_seconds
    )
    parser.add_argument("--database-url", default=settings.database_url)
    parser.add_argument("--execution", choices=["process", "container"], default=settings.execution)
    parser.add_argument("--task-image", default=settings.task_image)
    parser.add_argument("--deployment", default=settings.deployment)
    parser.add_argument("--exit-when-idle", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    run: Runner | Execution = load_runner(args.runner)
    if args.execution == "container":
        # the runner is named, not loaded: it is imported inside each task's container
        run = InContainer(
            Docker(settings.docker_socket),
            image=args.task_image,
            runner=args.runner,
            deployment=args.deployment,
            egress_network=settings.egress_network,
        )
    worker = asyncio.run(
        serve(
            url=args.database_url,
            run=run,
            worker_id=args.worker_id,
            lease_seconds=args.lease_seconds,
            heartbeat_seconds=args.heartbeat_seconds,
            reap_every_seconds=args.reap_every_seconds,
            retry=RetryPolicy(
                backoff_seconds=args.retry_backoff_seconds,
                backoff_cap_seconds=args.retry_backoff_cap_seconds,
            ),
            exit_when_idle=args.exit_when_idle,
        )
    )
    print(
        f"worker {worker.worker_id}: {worker.published} published, {worker.rejected} rejected, "
        f"{worker.lost} lost, {worker.released} released, {worker.cancelled} cancelled",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
