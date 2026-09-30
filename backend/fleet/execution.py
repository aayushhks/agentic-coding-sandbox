"""How a worker runs an attempt: in its own process, or in a locked-down container of its own."""

import asyncio
import contextlib
import json
import logging
import os
import shutil
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from fleet.containers import MANAGED, labels, proxy_config, task_config
from fleet.docker import Docker, DockerError
from fleet.models import ClaimedJob
from fleet.policy import ExecutionPolicy
from fleet.progress import reporting_to
from fleet.runners import Runner, RunnerError, RunnerOutcome

logger = logging.getLogger(__name__)
LOG_TAIL_CHARS = 16_000
PARTIAL_EVENTS = 50
# a result bigger than this is refused rather than read
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
DEPLOYMENT = "fleet.deployment"
LiveAttempts = Callable[[], Awaitable[set[tuple[int, int]]]]


@dataclass(slots=True)
class Attempted:
    """What an attempt produced, how it ran, and the tail of what it printed."""

    result: RunnerOutcome | RunnerError
    execution: dict[str, Any] | None = None
    logs: str | None = None


def cut_short(
    failure: str, policy: ExecutionPolicy, progress: list[dict[str, Any]], logs: str = ""
) -> RunnerOutcome:
    """A task stopped at one of its limits: a final failure that keeps what it had done."""
    return RunnerOutcome(
        outcome="failed",
        body={
            "failure": failure,
            "policy": policy.model_dump(mode="json"),
            "partial": {"events": len(progress), "last": progress[-PARTIAL_EVENTS:]},
            "logs": logs[-LOG_TAIL_CHARS:],
        },
    )


class Execution(ABC):
    @abstractmethod
    async def run(self, job: ClaimedJob, worker_id: str) -> Attempted: ...

    async def ready(self) -> None:
        """Refuse to start a worker that couldn't run anything."""
        return None

    async def reap_orphans(self, live: LiveAttempts) -> list[str]:
        """Stop what attempts without a live lease left running; the ids of what was stopped."""
        return []

    async def close(self) -> None:
        """Release what the execution holds."""
        return None


class InProcess(Execution):
    """The runner in the worker's own process, isolated only by the agent's sandbox."""

    def __init__(self, runner: Runner) -> None:
        self._runner = runner

    async def run(self, job: ClaimedJob, worker_id: str) -> Attempted:
        progress: list[dict[str, Any]] = []
        deadline = asyncio.timeout(job.policy.timeout_seconds)
        try:
            async with deadline:
                with reporting_to(progress.append):
                    outcome = await self._runner(job.name, job.payload)
        except Exception as exc:
            # only our own deadline is a timeout; a runner's own TimeoutError is infrastructure
            if isinstance(exc, TimeoutError) and deadline.expired():
                return Attempted(cut_short("timeout", job.policy, progress))
            return Attempted(RunnerError(f"{type(exc).__name__}: {exc}"))
        return Attempted(outcome)


def _read(path: Path) -> str | None:
    if not path.is_file() or path.stat().st_size > MAX_OUTPUT_BYTES:
        return None
    return path.read_text(errors="replace")


def _progress(path: Path) -> list[dict[str, Any]]:
    text = _read(path) or ""
    events = []
    for line in text.splitlines():
        with contextlib.suppress(ValueError):
            events.append(json.loads(line))
    return events


class InContainer(Execution):
    """Each attempt in a container of its own, with its policy's limits and nothing ungranted."""

    def __init__(
        self,
        docker: Docker,
        *,
        image: str,
        runner: str,
        deployment: str,
        egress_network: str = "bridge",
        work_root: Path | None = None,
    ) -> None:
        self._docker = docker
        self._image = image
        self._runner = runner
        self._deployment = deployment
        self._egress_network = egress_network
        self._work_root = work_root
        self._image_id: str | None = None

    async def ready(self) -> None:
        if not await self._docker.ping():
            raise RuntimeError("no docker daemon answers on its socket")
        self._image_id = await self._docker.image_id(self._image)
        if self._image_id is None:
            raise RuntimeError(f"no {self._image} image; build it with scripts/build-task-image.sh")

    def _labels(self, job: ClaimedJob, worker_id: str) -> dict[str, str]:
        tags = labels(job_id=job.id, attempt=job.attempt, worker_id=worker_id)
        return tags | {DEPLOYMENT: self._deployment}

    def _name(self, job: ClaimedJob) -> str:
        return f"fleet-{self._deployment}-{job.id}-{job.attempt}"

    async def _egress(self, job: ClaimedJob, tags: dict[str, str], created: list[str]) -> str:
        """A network of the attempt's own, whose only way out is a proxy for its grants."""
        network = self._name(job)
        await self._docker.create_network(network, internal=True, labels=tags)
        config = proxy_config(
            image=self._image, egress=job.policy.egress, network=network, labels=tags
        )
        config["NetworkingConfig"] = {"EndpointsConfig": {network: {"Aliases": ["egress"]}}}
        proxy = await self._docker.create(f"{network}-egress", config)
        created.append(proxy)
        await self._docker.connect(self._egress_network, proxy, aliases=[])
        await self._docker.start(proxy)
        # the task's first connection must not beat the proxy to its port
        for _ in range(200):
            if "listening" in await self._docker.logs(proxy):
                return network
            await asyncio.sleep(0.05)
        raise RuntimeError(f"the egress proxy for {network} never started listening")

    async def run(self, job: ClaimedJob, worker_id: str) -> Attempted:
        work = Path(tempfile.mkdtemp(prefix=f"fleet-{job.id}-{job.attempt}-", dir=self._work_root))
        inbox, outbox = work / "in", work / "out"
        created: list[str] = []
        network: str | None = None
        try:
            inbox.mkdir()
            outbox.mkdir()
            (inbox / "job.json").write_text(json.dumps({"name": job.name, "payload": job.payload}))
            # the task runs as another user: it may read its job, and write only its results
            for path, mode in ((work, 0o755), (inbox, 0o755), (inbox / "job.json", 0o644)):
                os.chmod(path, mode)
            os.chmod(outbox, 0o777)
            tags = self._labels(job, worker_id)
            if job.policy.egress:
                network = await self._egress(job, tags, created)
            container = await self._docker.create(
                self._name(job),
                task_config(
                    image=self._image,
                    runner=self._runner,
                    policy=job.policy,
                    input_dir=inbox,
                    output_dir=outbox,
                    network=network,
                    proxy="http://egress:3128" if network else None,
                    labels=tags,
                ),
            )
            created.append(container)
            started = time.monotonic()
            await self._docker.start(container)
            timed_out = False
            try:
                code = await asyncio.wait_for(
                    self._docker.wait(container), job.policy.timeout_seconds
                )
            except TimeoutError:
                timed_out = True
                await self._docker.kill(container)
                code = await self._docker.wait(container)
            seconds = time.monotonic() - started
            state = (await self._docker.inspect(container))["State"]
            logs = await self._docker.logs(container)
            usage = _read(outbox / "usage.json")
            execution = {
                "mode": "container",
                "image": self._image,
                "image_id": self._image_id,
                "container": container[:12],
                "policy": job.policy.model_dump(mode="json"),
                "network": "egress" if network else "none",
                "exit_code": code,
                "oom_killed": bool(state.get("OOMKilled")),
                "timed_out": timed_out,
                "seconds": round(seconds, 3),
                "usage": json.loads(usage) if usage else None,
            }
            result = self._result(job.policy, outbox, code, execution, logs)
            return Attempted(result, execution, logs[-LOG_TAIL_CHARS:])
        finally:
            # also on cancellation: a stopped run never leaves its container behind
            for item in reversed(created):
                with contextlib.suppress(DockerError, httpx.HTTPError):
                    await self._docker.remove(item)
            if network is not None:
                with contextlib.suppress(DockerError, httpx.HTTPError):
                    await self._docker.remove_network(network)
            shutil.rmtree(work, ignore_errors=True)

    def _result(
        self,
        policy: ExecutionPolicy,
        outbox: Path,
        code: int,
        execution: dict[str, Any],
        logs: str,
    ) -> RunnerOutcome | RunnerError:
        if execution["timed_out"]:
            return cut_short("timeout", policy, _progress(outbox / "progress.jsonl"), logs)
        result = _read(outbox / "result.json")
        if code == 0 and result is not None:
            return RunnerOutcome.model_validate_json(result)
        if execution["oom_killed"]:
            return cut_short("memory_limit", policy, _progress(outbox / "progress.jsonl"), logs)
        error = _read(outbox / "error.json")
        if error is not None:
            return RunnerError(str(json.loads(error)["error"]))
        last = logs.strip().splitlines()[-1] if logs.strip() else "no output"
        return RunnerError(f"task container exited {code}: {last}")

    async def reap_orphans(self, live: LiveAttempts) -> list[str]:
        mine = {MANAGED: "1", DEPLOYMENT: self._deployment}
        # listed before the live attempts are read, so a container created since can't look dead
        containers = await self._docker.containers(mine)
        networks = await self._docker.networks(mine)
        alive = await live()
        stopped = []
        for item in containers:
            tags = item["Labels"]
            if (int(tags["fleet.job"]), int(tags["fleet.attempt"])) not in alive:
                await self._docker.remove(item["Id"])
                stopped.append(str(item["Id"]))
        for network in networks:
            tags = network["Labels"]
            if (int(tags["fleet.job"]), int(tags["fleet.attempt"])) not in alive:
                with contextlib.suppress(DockerError):
                    await self._docker.remove_network(network["Id"])
        if stopped:
            logger.info("stopped %d containers whose attempts had lost their lease", len(stopped))
        return stopped

    async def close(self) -> None:
        await self._docker.aclose()
