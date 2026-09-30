"""Eight workers on one-second leases, killed and paused at random all through a 300-job batch."""

import asyncio
import random
import re
import signal
import statistics
import subprocess
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from fleet.invariants import check, snapshot
from fleet.models import NewJob
from fleet.store import batch_jobs, batch_status, submit_batch
from tests.fleet_helpers import start_worker

WORKERS = 8
JOBS = 300
LEASE_SECONDS = 1.0
MAX_ATTEMPTS = 3
PUBLISHED_AS = {"ok": "succeeded", "long": "succeeded", "flaky": "succeeded", "gives_up": "failed"}


def _plan(rng: random.Random, markers: Path) -> list[NewJob]:
    """Mostly short jobs, a few that outlive a lease, and some failing in each way there is."""
    jobs = []
    for n in range(JOBS):
        roll = rng.random()
        if roll < 0.05:
            kind, sleep_ms = "long", rng.randint(1200, 2000)
        elif roll < 0.10:
            kind, sleep_ms = "gives_up", rng.randint(20, 200)
        elif roll < 0.13:
            kind, sleep_ms = "crash", rng.randint(5, 50)
        elif roll < 0.16:
            kind, sleep_ms = "flaky", rng.randint(20, 200)
        else:
            kind, sleep_ms = "ok", rng.randint(20, 300)
        payload = {"kind": kind, "sleep_ms": sleep_ms, "marker": str(markers / f"job-{n}")}
        jobs.append(NewJob(name=f"job-{n}", payload=payload))
    return jobs


@dataclass
class _Fleet:
    url: str
    logs: Path
    processes: dict[str, "subprocess.Popen[str]"] = field(default_factory=dict)
    paused: set[str] = field(default_factory=set)
    kills: int = 0
    pauses: int = 0

    def start(self) -> None:
        worker_id = f"w{len(self.processes)}"
        # the child keeps its own handle on the log, so this one can close at once
        with (self.logs / f"{worker_id}.log").open("w") as log:
            self.processes[worker_id] = start_worker(
                self.url,
                runner="tests.fleet_helpers:chaos_runner",
                worker_id=worker_id,
                lease_seconds=LEASE_SECONDS,
                reap_every_seconds=0.5,
                retry_backoff_seconds=0.05,
                output=log,
            )

    def live(self) -> set[str]:
        return {name for name, process in self.processes.items() if process.poll() is None}

    async def kill(self, worker_id: str) -> None:
        self.kills += 1
        self.processes[worker_id].kill()
        await asyncio.to_thread(self.processes[worker_id].wait)
        self.start()

    async def pause(self, worker_id: str, seconds: float) -> None:
        self.pauses += 1
        self.paused.add(worker_id)
        self.processes[worker_id].send_signal(signal.SIGSTOP)
        await asyncio.sleep(seconds)
        self.processes[worker_id].send_signal(signal.SIGCONT)
        self.paused.discard(worker_id)

    async def stop(self) -> None:
        for worker_id in self.live():
            self.processes[worker_id].send_signal(signal.SIGTERM)
        for process in self.processes.values():
            await asyncio.to_thread(process.wait, 60)

    def log_text(self) -> str:
        return "".join(path.read_text() for path in sorted(self.logs.glob("*.log")))


async def _holding_a_running_job(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        rows = await connection.scalars(
            text("select distinct worker_id from fleet_jobs where state = 'running'")
        )
        return set(rows)


async def _chaos(engine: AsyncEngine, fleet: _Fleet, rng: random.Random, batch_id: int) -> None:
    """Until the batch is done, every so often kill or pause a worker that is mid-job."""
    pauses = []
    while not (status := await batch_status(engine, batch_id)) or not status.done:
        await asyncio.sleep(rng.uniform(0.3, 0.8))
        targets = sorted((await _holding_a_running_job(engine) & fleet.live()) - fleet.paused)
        if not targets:
            continue
        target = rng.choice(targets)
        if rng.random() < 0.5:
            await fleet.kill(target)
        else:
            # paused well past its lease, so the job is taken back before the worker wakes
            pauses.append(asyncio.create_task(fleet.pause(target, rng.uniform(1.5, 2.5))))
    await asyncio.gather(*pauses)


async def _recovery_seconds(engine: AsyncEngine) -> list[float]:
    """For each lapsed attempt, how long after its lease ran out the next attempt claimed it."""
    async with engine.connect() as connection:
        rows = await connection.scalars(
            text(
                "select extract(epoch from later.claimed_at - earlier.lease_expires_at) "
                "from fleet_attempts earlier join fleet_attempts later "
                "on later.job_id = earlier.job_id and later.attempt = earlier.attempt + 1 "
                "where earlier.ended_by = 'lease_expired'"
            )
        )
        return [float(seconds) for seconds in rows]


@pytest.mark.parametrize("seed", [0, 1])
async def test_eight_workers_on_short_leases_survive_kills_and_pauses(
    fleet_engine: AsyncEngine, fleet_database_url: str, tmp_path: Path, seed: int
) -> None:
    rng = random.Random(seed)
    (tmp_path / "markers").mkdir()
    (tmp_path / "logs").mkdir()
    planned = _plan(rng, tmp_path / "markers")
    submission = await submit_batch(
        fleet_engine, label=f"stress-{seed}", jobs=planned, max_attempts=MAX_ATTEMPTS
    )
    fleet = _Fleet(fleet_database_url, tmp_path / "logs")
    started = time.monotonic()
    for _ in range(WORKERS):
        fleet.start()
    try:
        await asyncio.wait_for(_chaos(fleet_engine, fleet, rng, submission.batch_id), timeout=240)
    finally:
        await fleet.stop()
    wall = time.monotonic() - started

    # the four invariants, read back from the database
    run = await snapshot(fleet_engine, submission.job_ids)
    violations = check(run, submission.job_ids)
    assert not violations, "\n".join(str(violation) for violation in violations)

    # every job ended the way its kind allows
    kinds = {job.name: job.payload["kind"] for job in planned}
    jobs = await batch_jobs(fleet_engine, submission.batch_id)
    endings: dict[int, list[str | None]] = defaultdict(list)
    for attempt in sorted(run.attempts, key=lambda row: row.attempt):
        endings[attempt.job_id].append(attempt.ended_by)
    for job in jobs:
        kind = kinds[job.name]
        if kind == "crash":
            assert (job.state, job.attempt) == ("dead_lettered", MAX_ATTEMPTS), job
            assert "released" in endings[job.id], job
        elif job.state != "dead_lettered":
            assert job.state == PUBLISHED_AS[kind], job
        if kind == "flaky" and job.state == "succeeded":
            assert job.attempt >= 2, job
    # jobs longer than a lease finished on their first attempt, carried by heartbeats
    assert any(kinds[job.name] == "long" and job.attempt == 1 for job in jobs)

    # the chaos happened, and fencing turned away workers that came back from a pause
    logs = fleet.log_text()
    lost, refused = logs.count("lost the lease"), logs.count("refused")
    assert fleet.kills >= 1 and fleet.pauses >= 1
    assert lost + refused >= 1

    states = Counter(job.state for job in jobs)
    ended = Counter(attempt.ended_by for attempt in run.attempts)
    recovery = await _recovery_seconds(fleet_engine)
    summaries = re.findall(r"worker \S+: .*", logs)
    print(
        f"\nseed {seed}: {JOBS} jobs, {WORKERS} workers, lease {LEASE_SECONDS}s, "
        f"{wall:.1f}s wall, {fleet.kills} kills, {fleet.pauses} pauses, "
        f"{len(fleet.processes)} worker processes in all"
        f"\n  final states {dict(sorted(states.items()))}"
        f"\n  attempt endings {dict(sorted(ended.items()))}, "
        f"{sum(job.attempt > 1 for job in jobs)} jobs ran more than once"
        f"\n  fenced off after a pause: {lost} lost leases, {refused} refused results"
        f"\n  a lapsed job was claimed again a median {statistics.median(recovery or [0]):.2f}s "
        f"(max {max(recovery or [0]):.2f}s) after its lease ran out, {len(recovery)} times"
        f"\n  {len(summaries)} workers reported at exit; violations: 0"
    )
