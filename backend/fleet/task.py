"""Run one job inside its task container, reporting back on this process's own stdout.

The job comes in at /in. Progress, usage and the result go out as tagged lines on stdout, each
carrying a token the worker gave this attempt. Nothing else in the container can forge one: this
process is the container's pid 1, it makes itself non-dumpable so same-user code can't read its
memory, environment or open files, and it drops the token from its environment before anything runs.
As pid 1 it forks once: the child runs the job, and the parent stays behind to reap orphans.
"""

import argparse
import asyncio
import ctypes
import json
import os
import resource
import sys
import threading
from pathlib import Path
from typing import Any

from fleet.progress import reporting_to
from fleet.runners import Runner, RunnerOutcome, load_runner

PR_SET_DUMPABLE = 4
TOKEN_VARIABLE = "FLEET_ATTEMPT_TOKEN"
TAG = "fleet"


def make_undumpable() -> None:
    """Make /proc/<pid> of this process root-owned, so same-user code can't read or trace it."""
    if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "could not make the task process non-dumpable")


class Channel:
    """Tagged lines on stdout, the only way anything gets back to the worker."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._lock = threading.Lock()

    def send(self, kind: str, **data: Any) -> None:
        line = json.dumps({TAG: kind, "token": self._token, **data})
        with self._lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()


async def _run(runner: Runner, job: dict[str, Any]) -> RunnerOutcome:
    return await runner(job["name"], job["payload"])


def _usage() -> dict[str, float]:
    """Peak memory of this process and of its largest child, and cpu used, for the record."""
    own, children = (
        resource.getrusage(who) for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)
    )
    return {
        "max_rss_mb": own.ru_maxrss / 1024,
        "max_child_rss_mb": children.ru_maxrss / 1024,
        "cpu_seconds": own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime,
    }


def _stop_at(seconds: float | None) -> None:
    """End the whole container at its deadline, even when the worker that should have is gone."""
    if seconds is None:
        return
    timer = threading.Timer(seconds, lambda: os._exit(137))
    timer.daemon = True
    timer.start()


def _reap_until(child: int) -> int:
    """Reap whatever is orphaned onto pid 1 until the job's own process exits; its exit code."""
    while True:
        pid, status = os.wait()
        if pid == child:
            code = os.waitstatus_to_exitcode(status)
            # killed by a signal: report it the way a shell would, 137 for a kill
            return 128 - code if code < 0 else code


def _run_job(runner_path: str, source: Path, channel: Channel) -> int:
    job = json.loads(source.read_text())
    runner = load_runner(runner_path)
    try:
        with reporting_to(lambda event: channel.send("progress", event=event)):
            outcome = asyncio.run(_run(runner, job))
    # the worker treats a runner that raised as an infrastructure failure
    except Exception as exc:
        channel.send("usage", usage=_usage())
        channel.send("error", error=f"{type(exc).__name__}: {exc}")
        return 1
    channel.send("usage", usage=_usage())
    channel.send("result", result=outcome.model_dump(mode="json"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", required=True, help="the job runner, as module:function")
    parser.add_argument("--input", type=Path, default=Path("/in/job.json"))
    parser.add_argument("--deadline-seconds", type=float, default=None)
    args = parser.parse_args(argv)
    make_undumpable()
    token = os.environ.pop(TOKEN_VARIABLE, "")
    if not token:
        print(f"no {TOKEN_VARIABLE}: nothing this task reported could be trusted", file=sys.stderr)
        return 2
    if os.getpid() == 1:
        # a container's first process inherits every orphan in it, and zombies would hold pids
        child = os.fork()
        if child:
            _stop_at(args.deadline_seconds)
            return _reap_until(child)
    else:
        _stop_at(args.deadline_seconds)
    return _run_job(args.runner, args.input, Channel(token))


if __name__ == "__main__":
    sys.exit(main())
