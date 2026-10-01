"""Run one scenario with one seed: submit, start workers armed to fail, supervise, then check.

The harness plays the part of a supervisor: it replaces workers that die, resumes paused ones once
their lease has run out, and restarts the api. For some scenarios it also restarts Postgres, claims
jobs as a worker that never comes back, or cancels running jobs. Once the batch has drained, it
reads everything back and checks three things: the invariants, the key on every result, and that
each fault left behind the effects its scenario says it must.
"""

import asyncio
import json
import os
import random
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bench.environment import REPO_ROOT, git_dirty
from bench.jobs import Outcome
from bench.replay import RECORDINGS_ROOT, LatencyProfile, load_recordings
from bench.runner import FLEET_OUTCOMES, replay_payload
from bench.taskset import load_taskset
from chaos.scenarios import Scenario
from fleet.client import FleetClient
from fleet.connections import ride_through
from fleet.containers import MANAGED
from fleet.docker import Docker
from fleet.execution import DEPLOYMENT
from fleet.failpoints import ARMED_VARIABLE, LOG_VARIABLE
from fleet.invariants import AttemptRow, JobRow, ResultRow, Snapshot, check, snapshot
from fleet.localdb import LocalPostgres
from fleet.models import UNFINISHED_STATES, NewJob
from fleet.store import cancel, claim, submit_batch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
JOBS = 30
LONG_JOBS = 18
WORKERS = 3
LEASE_SECONDS = 1.0
REAP_EVERY_SECONDS = 0.5
MAX_ATTEMPTS = 5
# how long a paused worker stays stopped: well past its lease, so its job is taken back meanwhile
PAUSE_SECONDS = (2.0, 2.5)
# the longest a worker waits to be overtaken before it is woken anyway, which fails the run
OVERTAKE_SECONDS = 15.0
# a worker that never comes back holds each job it claims this long
GHOST_LEASE_SECONDS = 0.3
# Postgres restarts once this share of the jobs has finished, and again, and again
RESTART_AT = (0.2, 0.45, 0.7)
TIMEOUT_SECONDS = 90.0
CHAOS_RUNNER = "chaos.runner:run"
AGENT_RUNNER = "bench.runner:run_job"


@dataclass
class Process:
    name: str
    popen: "subprocess.Popen[bytes]"
    armed: str | None
    log: Path
    replaced: bool = False


@dataclass
class Plan:
    """The batches to submit, and what their results have to say."""

    batches: list[list[NewJob]]
    # a keyed job's result must carry its key; an agent job's outcome must match its recording
    keys: dict[str, str] = field(default_factory=dict)
    outcomes: dict[str, str] = field(default_factory=dict)
    long_jobs: set[str] = field(default_factory=set)


def plan(scenario: Scenario, rng: random.Random) -> Plan:
    if scenario.workload == "agent":
        taskset = load_taskset()
        recordings = load_recordings(RECORDINGS_ROOT / taskset.version)
        jobs = [
            NewJob(
                name=task.id,
                payload=replay_payload(task, recordings[task.id], LatencyProfile.ZERO),
            )
            for task in taskset.tasks
        ]
        outcomes: dict[str, str] = {
            task.id: FLEET_OUTCOMES[Outcome(recordings[task.id].outcome)] for task in taskset.tasks
        }
        return Plan([jobs], outcomes=outcomes)
    jobs, keys, long_jobs = [], {}, set()
    for n in range(LONG_JOBS if scenario.workload == "long" else JOBS):
        name, key = f"job-{n}", uuid.UUID(int=rng.getrandbits(128)).hex
        kind, sleep_ms = "ok", rng.randint(200, 800)
        if scenario.workload == "long":
            sleep_ms = rng.randint(1200, 2000)
        if scenario.workload == "crash" and rng.random() < 0.3:
            kind = "crash"
        if scenario.workload == "cancel" and n % 3 == 0:
            # long enough to be running, and heartbeating, when the harness cancels it
            kind, sleep_ms = "ok", 3000
            long_jobs.add(name)
        keys[name] = key
        jobs.append(NewJob(name=name, payload={"key": key, "sleep_ms": sleep_ms, "kind": kind}))
    if scenario.workload == "api":
        return Plan([jobs[:10], jobs[10:20], jobs[20:]], keys=keys)
    return Plan([jobs], keys=keys, long_jobs=long_jobs)


@dataclass
class Evidence:
    """What a drained run left behind, for the effect checks."""

    taken: Snapshot
    names: dict[int, str]
    divergences: dict[int, str | None]
    jobs: dict[int, JobRow]
    attempts: dict[tuple[int, int], AttemptRow]
    results: dict[int, ResultRow]
    exits: dict[str, int | None]
    alive_at_end: set[str]
    logs: dict[str, str]
    submissions: dict[str, int]
    batches_by_key: dict[str, list[int]]


def _attempt(fault: dict[str, Any], seen: Evidence) -> AttemptRow | None:
    return seen.attempts.get((fault["job"], fault["attempt"]))


def _died(fault: dict[str, Any], seen: Evidence) -> str | None:
    code = seen.exits.get(fault["process"])
    return None if code == -signal.SIGKILL else f"{fault['process']} ended with {code}"


def _survived(fault: dict[str, Any], seen: Evidence) -> str | None:
    if fault["process"] in seen.alive_at_end:
        return None
    return f"{fault['process']} exited with {seen.exits.get(fault['process'])}"


def _claim_undone(fault: dict[str, Any], seen: Evidence) -> str | None:
    found = _attempt(fault, seen)
    if found is not None and found.worker_id == fault["process"]:
        return f"job {fault['job']} attempt {fault['attempt']} kept the interrupted claim"
    return None


def _retried(fault: dict[str, Any], seen: Evidence) -> str | None:
    found, job = _attempt(fault, seen), seen.jobs.get(fault["job"])
    if found is None or job is None or found.worker_id != fault["process"]:
        return f"job {fault['job']} has no attempt {fault['attempt']} by {fault['process']}"
    if found.ended_by != "lease_expired":
        return f"job {job.id} attempt {found.attempt} ended {found.ended_by}, not by its lease"
    if job.attempt <= found.attempt or job.state in UNFINISHED_STATES:
        return f"job {job.id} never finished on a later attempt ({job.state}, {job.attempt})"
    return None


def _same_attempt(fault: dict[str, Any], seen: Evidence) -> str | None:
    result = seen.results.get(fault["job"])
    if result is not None and result.attempt == fault["attempt"]:
        return None
    return f"job {fault['job']}'s result isn't from attempt {fault['attempt']}"


def _stands(fault: dict[str, Any], seen: Evidence) -> str | None:
    result, job = seen.results.get(fault["job"]), seen.jobs.get(fault["job"])
    if result is None or job is None or result.attempt != fault["attempt"]:
        return f"job {fault['job']}'s result isn't the one attempt {fault['attempt']} published"
    if job.attempt != fault["attempt"]:
        return f"job {job.id} ran again after its result was committed"
    return None


def _fenced(fault: dict[str, Any], seen: Evidence) -> str | None:
    if f"lost the lease on job {fault['job']}" in seen.logs.get(fault["process"], ""):
        return None
    return f"{fault['process']} never reported losing the lease on job {fault['job']}"


def _refusal(fault: dict[str, Any], seen: Evidence) -> bool:
    line = f"result for job {fault['job']} attempt {fault['attempt']} refused"
    return line in seen.logs.get(fault["process"], "")


def _refused(fault: dict[str, Any], seen: Evidence) -> str | None:
    if _refusal(fault, seen):
        return None
    return f"{fault['process']}'s late result for job {fault['job']} was never refused"


def _counted(fault: dict[str, Any], seen: Evidence) -> str | None:
    if not _refusal(fault, seen):
        return None
    return f"{fault['process']} counted its own committed result for job {fault['job']} as refused"


def _unblocked(fault: dict[str, Any], seen: Evidence) -> str | None:
    job, result = seen.jobs.get(fault["job"]), seen.results.get(fault["job"])
    if job is None or job.state in UNFINISHED_STATES:
        return f"job {fault['job']} was never finished while {fault['process']} hung holding it"
    if result is not None and result.worker_id == fault["process"]:
        return f"job {job.id} was finished only by the hung {fault['process']}"
    return None


def _reaped_once(fault: dict[str, Any], seen: Evidence) -> str | None:
    for job_id in fault["jobs"]:
        lapsed = [row for (job, _), row in seen.attempts.items() if job == job_id]
        ghost = [row for row in lapsed if row.worker_id == "ghost"]
        if not ghost or any(row.ended_by != "lease_expired" for row in ghost):
            return f"job {job_id}'s lapsed attempt wasn't ended by a reap"
        job = seen.jobs.get(job_id)
        if job is None or job.state in UNFINISHED_STATES:
            return f"job {job_id} never finished after its lease was reaped"
    return None


def _cancelled(fault: dict[str, Any], seen: Evidence) -> str | None:
    found, job = _attempt(fault, seen), seen.jobs.get(fault["job"])
    if job is None or job.state != "cancelled" or fault["job"] in seen.results:
        return f"job {fault['job']} didn't end cancelled without a result"
    if found is None or found.ended_by != "lease_expired":
        return f"job {job.id} attempt {fault['attempt']} wasn't ended by the reaper"
    return None


def _one_batch(fault: dict[str, Any], seen: Evidence) -> str | None:
    keys = [key for key, batch in seen.submissions.items() if batch == fault["batch"]]
    if len(keys) != 1:
        return f"the client never got batch {fault['batch']} back"
    if seen.batches_by_key.get(keys[0]) != [fault["batch"]]:
        return f"key {keys[0]} made batches {seen.batches_by_key.get(keys[0])}"
    return None


def _all_survived(fault: dict[str, Any], seen: Evidence) -> str | None:
    died = sorted(
        name for name in seen.exits if name.startswith("w") and name not in seen.alive_at_end
    )
    return f"{', '.join(died)} exited during the run" if died else None


EFFECT_CHECKS: dict[str, Callable[[dict[str, Any], Evidence], str | None]] = {
    "died": _died,
    "survived": _survived,
    "claim_undone": _claim_undone,
    "retried": _retried,
    "same_attempt": _same_attempt,
    "stands": _stands,
    "fenced": _fenced,
    "refused": _refused,
    "counted": _counted,
    "unblocked": _unblocked,
    "reaped_once": _reaped_once,
    "cancelled": _cancelled,
    "one_batch": _one_batch,
    "all_survived": _all_survived,
}


class Run:
    def __init__(
        self,
        scenario: Scenario,
        seed: int,
        *,
        url: str,
        cluster: LocalPostgres | None,
        workdir: Path,
        image: str,
    ) -> None:
        self.scenario = scenario
        self.seed = seed
        self.rng = random.Random(f"{scenario.name}/{seed}")
        self.url = url
        self.cluster = cluster
        self.workdir = workdir
        self.image = image
        self.deployment = f"chaos-{uuid.uuid4().hex[:8]}"
        self.fault_log = workdir / "faults.jsonl"
        self.arms = scenario.arms(self.rng)
        self.planned = len(self.arms) or scenario.faults
        self.processes: dict[str, Process] = {}
        self.api: Process | None = None
        self.api_port = 0
        self.api_starts = 0
        self.faults: list[dict[str, Any]] = []
        self._read = 0
        self.resume_at: dict[int, float] = {}
        # paused workers to wake once a later attempt holds their job: pid to fault and deadline
        self.overtake: dict[int, tuple[dict[str, Any], float]] = {}
        self.hung: set[int] = set()
        self.started = time.time()
        # the build this run tested, read before anything runs
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
        )
        self.build = {"git_sha": sha.stdout.strip() or "unknown", "git_dirty": git_dirty(REPO_ROOT)}
        self.job_ids: list[int] = []
        self.batches = 0
        self.submissions: dict[str, int] = {}
        self.restarts = 0
        self.ghost_claims = 0
        self.ghost_at = 0.0
        self.cancelled: set[int] = set()

    def _spawn(self, name: str, args: list[str], armed: str | None, env: dict[str, str]) -> Process:
        log = self.workdir / f"{name}.log"
        with log.open("wb") as handle:
            popen = subprocess.Popen(
                [sys.executable, *args],
                cwd=BACKEND_ROOT,
                env={
                    **os.environ,
                    "PYTHONPATH": str(BACKEND_ROOT),
                    ARMED_VARIABLE: armed or "",
                    LOG_VARIABLE: str(self.fault_log),
                    **env,
                },
                stdout=handle,
                stderr=subprocess.STDOUT,
            )
        return Process(name, popen, armed, log)

    def start_worker(self) -> None:
        name = f"w{len(self.processes)}"
        armed = self.arms.pop(0) if self.arms else None
        runner = AGENT_RUNNER if self.scenario.workload == "agent" else CHAOS_RUNNER
        args = [
            *("-m", "fleet.worker", "--runner", runner, "--worker-id", name),
            *("--lease-seconds", str(LEASE_SECONDS)),
            *("--reap-every-seconds", str(REAP_EVERY_SECONDS)),
            *("--retry-backoff-seconds", "0.05", "--db-retry-seconds", "30"),
            *("--database-url", self.url),
        ]
        if self.scenario.execution == "container":
            args += ["--execution", "container", "--task-image", self.image]
            args += ["--deployment", self.deployment]
        self.processes[name] = self._spawn(name, args, armed, {})

    def start_api(self) -> None:
        # every api that starts while the faults aren't spent dies after its first new batch
        armed = "api.submit.after_commit=kill@1" if self.api_starts < self.planned else None
        name = f"api{self.api_starts}"
        self.api_starts += 1
        args = ["-m", "uvicorn", "fleet.api:app", "--host", "127.0.0.1"]
        args += ["--port", str(self.api_port), "--log-level", "warning", "--no-access-log"]
        process = self._spawn(name, args, armed, {"FLEET_DATABASE_URL": self.url})
        self.processes[name] = process
        self.api = process

    def read_faults(self) -> None:
        if not self.fault_log.exists():
            return
        lines = self.fault_log.read_text().splitlines()
        names = {process.popen.pid: name for name, process in self.processes.items()}
        for line in lines[self._read :]:
            fault = json.loads(line)
            fault["process"] = names.get(fault["pid"], "harness")
            fault["at"] = round(fault["at"] - self.started, 3)
            self.faults.append(fault)
            if fault["action"] == "stop":
                if self.scenario.action == "hang":
                    self.hung.add(fault["pid"])
                elif self.scenario.wake == "overtaken":
                    self.overtake[fault["pid"]] = (fault, time.monotonic() + OVERTAKE_SECONDS)
                else:
                    pause = self.rng.uniform(*PAUSE_SECONDS)
                    self.resume_at[fault["pid"]] = time.monotonic() + pause
        self._read = len(lines)

    def resume_due(self) -> None:
        for pid, at in list(self.resume_at.items()):
            if time.monotonic() >= at:
                os.kill(pid, signal.SIGCONT)
                del self.resume_at[pid]

    def replace_the_dead(self) -> None:
        fired = sum(fault["point"] == self.scenario.point for fault in self.faults)
        holding = self.scenario.replace == "after_faults" and fired < self.planned
        for process in list(self.processes.values()):
            if process.popen.poll() is None or process.replaced:
                continue
            if holding and process is not self.api:
                continue
            process.replaced = True
            if process is self.api:
                self.start_api()
            elif not process.name.startswith("api"):
                self.start_worker()

    def note(self, point: str, **context: Any) -> None:
        """A fault the harness injects itself goes in the same log as those failpoints fire."""
        line = {"point": point, "action": "restart", "pid": -1, "at": time.time(), **context}
        with self.fault_log.open("a") as handle:
            handle.write(json.dumps(line) + "\n")


async def _db[T](call: Callable[[], Awaitable[T]]) -> T:
    # the harness's own queries ride through a Postgres restart, as the workers' do
    return await ride_through(call, seconds=30)


async def _rows(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async def query() -> list[Any]:
        async with engine.connect() as connection:
            return list(await connection.execute(text(sql), params))

    return await _db(query)


async def _finished(engine: AsyncEngine, job_ids: list[int]) -> tuple[int, int]:
    rows = await _rows(
        engine,
        "select count(*) filter (where state <> all(:open)) from fleet_jobs where id = any(:ids)",
        open=sorted(UNFINISHED_STATES),
        ids=job_ids,
    )
    return int(rows[0][0]), len(job_ids)


async def _drive(run: Run, engine: AsyncEngine, plan_: Plan) -> None:
    """The faults a scenario makes from outside: cancels, ghost claims and Postgres restarts."""
    workload, now = run.scenario.workload, time.monotonic()
    if workload == "cancel":
        for row in await _rows(
            engine,
            "select id from fleet_jobs where id = any(:ids) and state = 'running' "
            "and name = any(:names)",
            ids=run.job_ids,
            names=sorted(plan_.long_jobs),
        ):
            if row.id not in run.cancelled:
                run.cancelled.add(row.id)
                await _db(partial(cancel, engine, row.id))
    reaps = sum(fault["point"] == "reap.before_commit" for fault in run.faults)
    if workload == "ghost" and reaps < run.planned and now >= run.ghost_at:
        # a worker that claims a job and is never heard from again leaves a lease to reap
        if await _db(lambda: claim(engine, worker_id="ghost", lease_seconds=GHOST_LEASE_SECONDS)):
            run.ghost_claims += 1
        run.ghost_at = now + 0.4
    if run.scenario.point == "postgres.restart" and run.cluster is not None:
        done, total = await _finished(engine, run.job_ids)
        if run.restarts < len(RESTART_AT) and done >= RESTART_AT[run.restarts] * total:
            run.note("postgres.restart", finished=done)
            await asyncio.to_thread(run.cluster.restart)
            run.restarts += 1


async def _submit_through_api(run: Run, plan_: Plan, client: FleetClient) -> None:
    for number, jobs in enumerate(plan_.batches):
        key = f"chaos-{run.scenario.name}-{run.seed}-{number}"
        submission = await client.submit(
            label=key, jobs=jobs, idempotency_key=key, max_attempts=MAX_ATTEMPTS, retry_seconds=60
        )
        run.submissions[key] = submission.batch_id
        run.job_ids += submission.job_ids
        run.batches += 1


async def _containers_left(run: Run) -> int:
    docker = Docker()
    try:
        deadline = time.monotonic() + 20
        while left := await docker.containers({MANAGED: "1", DEPLOYMENT: run.deployment}):
            # a dead worker's container goes once its lease lapses and another worker reaps it
            if time.monotonic() > deadline:
                return len(left)
            await asyncio.sleep(0.2)
        return 0
    finally:
        await docker.aclose()


async def _wake_overtaken(run: Run, engine: AsyncEngine) -> None:
    for pid, (fault, deadline) in list(run.overtake.items()):
        held = await _rows(
            engine,
            "select 1 from fleet_jobs where id = :job and attempt > :attempt "
            "and state in ('claimed', 'running')",
            job=fault["job"],
            attempt=fault["attempt"],
        )
        if held or time.monotonic() > deadline:
            fault["woke"] = "overtaken" if held else "never overtaken"
            os.kill(pid, signal.SIGCONT)
            del run.overtake[pid]


def _stop_all(run: Run) -> set[str]:
    """Stop every process, and say which were still alive, and had been all along, at the end."""
    alive = {name for name, process in run.processes.items() if process.popen.poll() is None}
    for pid in run.hung:
        # a hung worker is gone for good: nothing it holds is ever let go by it
        os.kill(pid, signal.SIGKILL)
    for pid in [*run.resume_at, *run.overtake]:
        os.kill(pid, signal.SIGCONT)
    for process in run.processes.values():
        if process.popen.poll() is None:
            process.popen.send_signal(signal.SIGTERM)
    for process in run.processes.values():
        try:
            process.popen.wait(30)
        except subprocess.TimeoutExpired:
            process.popen.kill()
            process.popen.wait()
    hung = {name for name, process in run.processes.items() if process.popen.pid in run.hung}
    return alive - hung


async def _evidence(run: Run, engine: AsyncEngine, alive: set[str]) -> Evidence:
    taken = await snapshot(engine, run.job_ids)
    named = await _rows(
        engine, "select id, name from fleet_jobs where id = any(:ids)", ids=run.job_ids
    )
    diverged = await _rows(
        engine,
        "select job_id, body->>'divergence' as divergence from fleet_results "
        "where job_id = any(:ids)",
        ids=run.job_ids,
    )
    keyed = await _rows(
        engine, "select idempotency_key, id from fleet_batches where idempotency_key is not null"
    )
    batches_by_key: dict[str, list[int]] = {}
    for row in keyed:
        batches_by_key.setdefault(row.idempotency_key, []).append(row.id)
    return Evidence(
        taken=taken,
        names={row.id: row.name for row in named},
        divergences={row.job_id: row.divergence for row in diverged},
        jobs={job.id: job for job in taken.jobs},
        attempts={(row.job_id, row.attempt): row for row in taken.attempts},
        results={row.job_id: row for row in taken.results},
        exits={name: process.popen.returncode for name, process in run.processes.items()},
        alive_at_end=alive,
        logs={
            name: process.log.read_text(errors="replace") for name, process in run.processes.items()
        },
        submissions=run.submissions,
        batches_by_key=batches_by_key,
    )


async def run_scenario(
    scenario: Scenario,
    seed: int,
    *,
    url: str,
    cluster: LocalPostgres | None,
    workdir: Path,
    image: str = "fleet-task:local",
) -> dict[str, Any]:
    """One run, from an empty store to a record of what was injected and what the checks found."""
    run = Run(scenario, seed, url=url, cluster=cluster, workdir=workdir, image=image)
    plan_ = plan(scenario, run.rng)
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "truncate fleet_results, fleet_attempts, fleet_jobs, fleet_batches "
                    "restart identity cascade"
                )
            )
        submitting: asyncio.Task[None] | None = None
        if scenario.workload == "api":
            run.api_port = _free_port()
            run.start_api()
            client = FleetClient(f"http://127.0.0.1:{run.api_port}")
            submitting = asyncio.create_task(_submit_through_api(run, plan_, client))
        else:
            for jobs in plan_.batches:
                submission = await submit_batch(
                    engine, label=scenario.name, jobs=jobs, max_attempts=MAX_ATTEMPTS
                )
                run.job_ids += submission.job_ids
                run.batches += 1
        for _ in range(WORKERS + scenario.spares):
            run.start_worker()
        drained = False
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            run.read_faults()
            run.resume_due()
            await _wake_overtaken(run, engine)
            run.replace_the_dead()
            if submitting is not None and submitting.done():
                submitting.result()
            await _drive(run, engine, plan_)
            done, total = await _finished(engine, run.job_ids)
            waking = run.resume_at or run.overtake
            if run.batches == len(plan_.batches) and done == total and not waking:
                drained = True
                break
            await asyncio.sleep(0.05)
        if submitting is not None and not submitting.done():
            submitting.cancel()
        left = await _containers_left(run) if scenario.execution == "container" else 0
        run.read_faults()
        alive = await asyncio.to_thread(_stop_all, run)
        seen = await _evidence(run, engine, alive)
        return _record(run, plan_, seen, drained=drained, containers_left=left)
    finally:
        await engine.dispose()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def _outcomes(plan_: Plan, seen: Evidence) -> list[str]:
    """An agent job must publish what its recording did, and never stray from it."""
    found = []
    for job_id, name in sorted(seen.names.items()):
        expected = plan_.outcomes.get(name)
        if expected is None:
            continue
        result = seen.results.get(job_id)
        if result is None or result.outcome != expected:
            published = None if result is None else result.outcome
            found.append(f"job {job_id} ({name}) published {published}, its recording {expected}")
        if seen.divergences.get(job_id):
            found.append(f"job {job_id} ({name}) diverged: {seen.divergences[job_id]}")
    return found


def _troubled(violations: dict[str, list[str]], seen: Evidence) -> dict[str, list[str]]:
    named = {
        int(match)
        for found in violations.values()
        for problem in found
        for match in re.findall(r"job (\d+)", problem)
    }
    return {
        str(job_id): [
            f"attempt {row.attempt} by {row.worker_id} ended {row.ended_by}"
            for (job, _), row in sorted(seen.attempts.items())
            if job == job_id
        ]
        for job_id in sorted(named)[:5]
    }


def _record(
    run: Run, plan_: Plan, seen: Evidence, *, drained: bool, containers_left: int
) -> dict[str, Any]:
    scenario = run.scenario
    keys = {job_id: plan_.keys[name] for job_id, name in seen.names.items() if name in plan_.keys}
    invariants = [str(violation) for violation in check(seen.taken, run.job_ids, keys or None)]
    own = [fault for fault in run.faults if fault["point"] == scenario.point]
    effects = [
        f"{effect}: {problem}"
        for fault in own
        for effect in scenario.effects
        if (problem := EFFECT_CHECKS[effect](fault, seen)) is not None
    ]
    chaos = []
    if len(own) != run.planned:
        chaos.append(f"{len(own)} of the {run.planned} planned faults happened")
    chaos += [
        f"{fault['process']} woke before a later attempt took job {fault['job']} over"
        for fault in own
        if scenario.wake == "overtaken" and fault.get("woke") != "overtaken"
    ]
    if not drained:
        chaos.append(f"the batch did not drain within {TIMEOUT_SECONDS:.0f} s")
    if containers_left:
        chaos.append(f"{containers_left} task containers were left behind")
    violations = {
        "invariants": invariants,
        "outcomes": _outcomes(plan_, seen),
        "effects": effects,
        "chaos": chaos,
    }
    return {
        "scenario": scenario.name,
        "seed": run.seed,
        "point": scenario.point,
        "action": scenario.action,
        "summary": scenario.summary,
        "workload": scenario.workload,
        "execution": scenario.execution,
        "effects": list(scenario.effects),
        "build": run.build,
        "config": {
            "jobs": len(run.job_ids),
            "workers": WORKERS + scenario.spares,
            "lease_seconds": LEASE_SECONDS,
            "heartbeat_seconds": round(LEASE_SECONDS / 3, 3),
            "reap_every_seconds": REAP_EVERY_SECONDS,
            "max_attempts": MAX_ATTEMPTS,
            "faults_planned": run.planned,
        },
        "faults": run.faults,
        "faults_injected": len(own),
        "drained": drained,
        "wall_seconds": round(time.time() - run.started, 2),
        "violations": violations,
        "counts": {
            "final_states": dict(sorted(Counter(job.state for job in seen.jobs.values()).items())),
            "attempt_endings": dict(
                sorted(Counter(str(row.ended_by) for row in seen.attempts.values()).items())
            ),
            "jobs_run_more_than_once": sum(job.attempt > 1 for job in seen.jobs.values()),
            "processes_started": len(run.processes),
            "ghost_claims": run.ghost_claims,
            "postgres_restarts": run.restarts,
            "cancels": len(run.cancelled),
        },
        "passed": not any(violations.values()),
        # how each job a violation names ran, so a failure can be read from the record alone
        "troubled_jobs": _troubled(violations, seen),
    }
