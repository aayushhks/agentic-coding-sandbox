"""Runners and process helpers shared by the fleet tests."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from fleet.worker import RunnerOutcome

BACKEND_ROOT = Path(__file__).resolve().parents[1]


async def sleep_runner(name: str, payload: dict[str, Any]) -> RunnerOutcome:
    """A stand-in job: sleeps for payload["sleep_ms"] and reports what it did."""
    sleep_ms = int(payload.get("sleep_ms", 0))
    await asyncio.sleep(sleep_ms / 1000)
    return RunnerOutcome(outcome="succeeded", body={"name": name, "slept_ms": sleep_ms})


def start_worker(
    url: str,
    *,
    runner: str = "tests.fleet_helpers:sleep_runner",
    worker_id: str = "w0",
    lease_seconds: float = 60.0,
    exit_when_idle: bool = False,
) -> "subprocess.Popen[str]":
    """Start `python -m fleet.worker` as its own process, the way a deployment would."""
    command = [
        sys.executable,
        "-m",
        "fleet.worker",
        "--runner",
        runner,
        "--worker-id",
        worker_id,
        "--lease-seconds",
        str(lease_seconds),
        "--database-url",
        url,
    ]
    if exit_when_idle:
        command.append("--exit-when-idle")
    return subprocess.Popen(
        command,
        cwd=BACKEND_ROOT,
        env={**os.environ, "PYTHONPATH": str(BACKEND_ROOT)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
