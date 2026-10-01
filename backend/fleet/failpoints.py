"""Named points in the fleet's protocol where a chaos run can inject a fault; inert unless armed.

A process is armed through FLEET_FAILPOINTS, as point=action@hit pairs separated by commas:
"publish.before_commit=kill@3" makes the third arrival at that point kill the process. Each armed
point fires once. Before it acts, it appends what it is about to do to the file named by
FLEET_FAILPOINT_LOG, so a run counts the faults that happened rather than the ones it planned.

- kill: the process sends itself SIGKILL;
- stop: the process sends itself SIGSTOP, and whoever reads the log resumes it;
- drop: the database connection goes away. Inside a transaction, Postgres terminates it, so the
  transaction never commits; anywhere else, the caller gets the error a dropped connection raises,
  as if the answer to a committed transaction had been lost on the way back.
"""

import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

ARMED_VARIABLE = "FLEET_FAILPOINTS"
LOG_VARIABLE = "FLEET_FAILPOINT_LOG"
ACTIONS = ("kill", "stop", "drop")
# every point, in the order a job meets them, and where each one is
POINTS = {
    "api.submit.after_commit": "a batch is committed, and the api hasn't answered yet",
    "claim.before": "a worker is about to claim",
    "claim.before_commit": "a claim is written, not yet committed",
    "claim.after_commit": "a claim is committed, and the worker hasn't started the job",
    "start.after_commit": "a job is marked running, and its run hasn't begun",
    "heartbeat.before": "a job is running, and its lease is about to be extended",
    "publish.before_commit": "a result is written, not yet committed",
    "publish.after_commit": "a result is committed, and the worker hasn't moved on",
    "release.before_commit": "a job is given back after a failure, not yet committed",
    "release.after_commit": "a job given back is committed, and the worker hasn't moved on",
    "reap.before_commit": "lapsed leases are ended, not yet committed",
    "cancel.before_commit": "a cancelled job is ended, not yet committed",
    "cancel.after_commit": "a cancelled job's end is committed, and the worker hasn't moved on",
}


class FailpointError(ValueError):
    """A failpoint spec names a point, action or hit that doesn't exist."""


@dataclass(slots=True)
class Armed:
    action: str
    hit: int
    seen: int = 0


def parse(spec: str) -> dict[str, Armed]:
    """point=action@hit pairs, separated by commas; an empty spec arms nothing."""
    armed = {}
    for item in filter(None, (part.strip() for part in spec.split(","))):
        point, _, rest = item.partition("=")
        action, _, hit = rest.partition("@")
        if point not in POINTS:
            raise FailpointError(f"no failpoint called {point!r}")
        if action not in ACTIONS:
            raise FailpointError(f"{point}: no action {action!r}; one of {', '.join(ACTIONS)}")
        if not hit.isdigit() or int(hit) < 1:
            raise FailpointError(f"{point}: the hit to fire on must be a number from 1")
        armed[point] = Armed(action, int(hit))
    return armed


_armed: dict[str, Armed] = parse(os.environ.get(ARMED_VARIABLE, ""))
_log: Path | None = Path(os.environ[LOG_VARIABLE]) if os.environ.get(LOG_VARIABLE) else None


def arm(spec: str, log: Path | None = None) -> None:
    """Arm this process's points afresh; a chaos run does the same through its environment."""
    global _armed, _log
    _armed, _log = parse(spec), log


def _record(point: str, action: str, context: dict[str, object]) -> None:
    if _log is None:
        return
    fired = {"point": point, "action": action, "pid": os.getpid(), "at": time.time(), **context}
    line = json.dumps(fired) + "\n"
    # one write to a file opened for appending, so a kill right after can't lose or tear it
    fd = os.open(_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


async def failpoint(
    point: str, connection: AsyncConnection | None = None, **context: object
) -> None:
    """Do nothing, unless this point is armed and this is the hit it fires on."""
    armed = _armed.get(point)
    if armed is None:
        return
    armed.seen += 1
    if armed.seen != armed.hit:
        return
    del _armed[point]
    _record(point, armed.action, context)
    if armed.action == "kill":
        os.kill(os.getpid(), signal.SIGKILL)
    elif armed.action == "stop":
        os.kill(os.getpid(), signal.SIGSTOP)
    elif connection is not None:
        await connection.execute(text("select pg_terminate_backend(pg_backend_pid())"))
    else:
        raise ConnectionResetError(f"failpoint {point}: the connection dropped")
