"""Probe jobs that check what a task container enforces, by trying what it should refuse.

Run them through the fleet like any job (runner fleet.probes:run). Each reports what it observed:
- sleep: sleeps, reporting progress as it goes, to see a timeout keep the partial result;
- allocate: holds more memory than its limit allows, to see it killed and reported;
- busy: burns cpu for a while and reports how much it got;
- connect: tries a destination directly and through the egress proxy;
- sandbox: runs commands in the agent's sandbox, as generated code would, and reports what they
  could reach: the network, the job, the task process and its channel;
- orphans: leaves processes behind for the container's pid 1 to adopt, and counts the zombies;
- raise: fails underneath the task.
"""

import asyncio
import os
import resource
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fleet.progress import report
from fleet.runners import RunnerOutcome


def _cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def _connect(host: str, port: int) -> str:
    try:
        with socket.create_connection((host, port), timeout=3):
            return "connected"
    except OSError as exc:
        return f"refused: {exc}"


def _through_proxy(host: str, port: int) -> str:
    proxy = os.environ.get("HTTPS_PROXY")
    if not proxy:
        return "no proxy"
    address = urlparse(proxy)
    try:
        with socket.create_connection((address.hostname, address.port or 80), timeout=3) as sock:
            sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
            return sock.recv(64).split(b"\r\n", 1)[0].decode(errors="replace")
    except OSError as exc:
        return f"refused: {exc}"


def _zombies() -> int:
    """Processes in this pid namespace that have exited and that nobody has reaped."""
    count = 0
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            # the state follows the command name, which is in parentheses and may hold spaces
            count += stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"
        except OSError:
            continue
    return count


def _sandbox(commands: dict[str, str]) -> dict[str, str]:
    # imported here: only this probe needs the agent's sandbox
    from app.sandbox.subprocess_sandbox import SubprocessSandbox
    from app.sandbox.tools import ToolCall, ToolName

    sandbox = SubprocessSandbox()
    try:
        seen = {"isolation": sandbox.isolation}
        for name, command in commands.items():
            # {runner} stands for this process, which generated code should not be able to reach
            command = command.replace("{runner}", str(os.getpid()))
            result = sandbox.execute(ToolCall(ToolName.RUN_COMMAND, {"command": command}))
            seen[name] = result.output.strip()
        return seen
    finally:
        sandbox.cleanup()


async def run(name: str, payload: dict[str, Any]) -> RunnerOutcome:
    probe = payload["probe"]
    body: dict[str, Any] = {"probe": probe}
    if probe == "sleep":
        steps = round(payload["seconds"] / 0.1)
        for step in range(steps):
            report({"step": step})
            await asyncio.sleep(0.1)
        body["steps"] = steps
    elif probe == "allocate":
        held = bytearray(int(payload["mb"]) * 1024 * 1024)
        # touch every page so the memory is really used, not just reserved
        for at in range(0, len(held), 4096):
            held[at] = 1
        body["allocated_mb"] = payload["mb"]
    elif probe == "busy":
        # only the loop counts, not the cpu the process spent starting up
        cpu_before, started = _cpu_seconds(), time.monotonic()
        while time.monotonic() - started < payload["seconds"]:
            pass
        body |= {"wall": time.monotonic() - started, "cpu": _cpu_seconds() - cpu_before}
    elif probe == "connect":
        host, port = payload["host"], int(payload["port"])
        body |= {"direct": _connect(host, port), "proxy": _through_proxy(host, port)}
    elif probe == "sandbox":
        body |= _sandbox(payload["commands"])
    elif probe == "orphans":
        # each shell exits at once, so its sleep is adopted by pid 1 and exits there
        for _ in range(payload["count"]):
            shell = await asyncio.create_subprocess_exec("sh", "-c", "sleep 0.2 &")
            await shell.wait()
        await asyncio.sleep(1)
        body["zombies"] = _zombies()
    elif probe == "raise":
        raise RuntimeError("the probe failed underneath the task")
    else:
        raise ValueError(f"no such probe: {probe}")
    return RunnerOutcome(outcome="succeeded", body=body)
