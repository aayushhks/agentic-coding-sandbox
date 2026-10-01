"""Where the cpu went during a batch: the host's as a whole, and each fleet process tree's.

The host's comes from /proc/stat, read on a timer for as long as the batch runs. A process tree's
is the cpu time of its root and of every live descendant, plus what the kernel has already folded
into them from descendants that exited and were waited for (cutime and cstime), read from
/proc/<pid>/stat as the batch starts and again as it ends.
"""

import asyncio
import contextlib
import os
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from pydantic import BaseModel

PROC = Path("/proc")
TICKS_PER_SECOND = os.sysconf("SC_CLK_TCK")

Clock = Callable[[], float]
ProcessTable = Mapping[int, tuple[int, int]]


@dataclass(frozen=True, slots=True)
class HostCpu:
    """Ticks spent since boot, summed over every cpu, and the tasks runnable when it was read."""

    busy: int
    idle: int
    steal: int
    runnable: int
    cpus: int

    @property
    def total(self) -> int:
        return self.busy + self.idle + self.steal


def parse_stat(text: str) -> HostCpu:
    busy = idle = steal = runnable = cpus = 0
    for line in text.splitlines():
        name, *values = line.split() or [""]
        if name == "cpu":
            # guest time is already counted inside user, so only the first eight columns add up
            user, nice, system, idle_, iowait, irq, softirq, stolen = map(int, values[:8])
            busy, idle, steal = user + nice + system + irq + softirq, idle_ + iowait, stolen
        elif name.startswith("cpu"):
            cpus += 1
        elif name == "procs_running":
            runnable = int(values[0])
    return HostCpu(busy, idle, steal, runnable, cpus)


def parse_pressure(text: str) -> float:
    """Seconds since boot in which some runnable task waited for a cpu, from /proc/pressure/cpu."""
    for line in text.splitlines():
        kind, *fields = line.split() or [""]
        if kind == "some":
            values = dict(field.split("=", 1) for field in fields)
            return int(values["total"]) / 1_000_000
    raise ValueError("the cpu pressure file has no 'some' line")


def parse_process(text: str) -> tuple[int, int]:
    """A process's parent, and the cpu ticks it used plus those of the children it waited for."""
    # the command name sits in parentheses and may itself hold spaces and parentheses
    fields = text[text.rindex(")") + 2 :].split()
    utime, stime, cutime, cstime = map(int, fields[11:15])
    return int(fields[1]), utime + stime + cutime + cstime


def processes(proc: Path = PROC) -> dict[int, tuple[int, int]]:
    """Every process's parent and cpu ticks; one that exits mid-scan is left out."""
    table = {}
    for entry in proc.iterdir():
        if entry.name.isdigit():
            with contextlib.suppress(OSError, ValueError):
                table[int(entry.name)] = parse_process((entry / "stat").read_text())
    return table


def named(names: Iterable[str], proc: Path = PROC) -> list[int]:
    """The processes whose command name is one of these, the docker daemons for instance."""
    wanted = set(names)
    found = []
    for entry in proc.iterdir():
        if entry.name.isdigit():
            with contextlib.suppress(OSError):
                if (entry / "comm").read_text().strip() in wanted:
                    found.append(int(entry.name))
    return sorted(found)


def tree_ticks(table: ProcessTable, roots: Sequence[int]) -> int | None:
    """Cpu ticks of the roots and of all their descendants; None if any root is not running."""
    if not roots or any(root not in table for root in roots):
        return None
    children = defaultdict(list)
    for pid, (parent, _) in table.items():
        children[parent].append(pid)
    seen: set[int] = set()
    pending = list(roots)
    while pending:
        pid = pending.pop()
        if pid not in seen:
            seen.add(pid)
            pending.extend(children[pid])
    return sum(table[pid][1] for pid in seen)


def _own_cpu() -> float:
    times = os.times()
    return times.user + times.system


class BatchResources(BaseModel):
    """The cpu a batch used, from just before it was submitted until it was seen to be done."""

    window_seconds: float
    cpus: int
    sample_seconds: float
    # busy time added up over every cpu, so it can exceed the window
    host_busy_cpu_seconds: float
    host_busy_fraction: float
    host_steal_fraction: float
    # the share of the window in which some runnable task waited for a cpu; None without PSI
    cpu_wait_fraction: float | None
    # cpu seconds of each process tree, None when a root was not running at either end
    processes: dict[str, float | None]
    # per sample: seconds into the window, the host's busy fraction since the last, tasks runnable
    timeline: list[tuple[float, float, int]]


class Sampler:
    """Reads the host's cpu on a timer between enter and exit, and each tree's at both ends."""

    def __init__(
        self,
        groups: Mapping[str, Sequence[int]],
        *,
        interval: float = 0.5,
        proc: Path = PROC,
        clock: Clock = time.monotonic,
    ) -> None:
        self._groups = groups
        self._interval = interval
        self._proc = proc
        self._clock = clock
        self._samples: list[tuple[float, HostCpu]] = []
        self._trees_before: dict[str, int | None] = {}
        self._own_before = 0.0
        self._pressure_before: float | None = None
        self._task: asyncio.Task[None] | None = None
        self.result: BatchResources | None = None

    def _host(self) -> HostCpu:
        return parse_stat((self._proc / "stat").read_text())

    def _pressure(self) -> float | None:
        try:
            return parse_pressure((self._proc / "pressure" / "cpu").read_text())
        except (OSError, ValueError):
            return None

    def _trees(self) -> dict[str, int | None]:
        table = processes(self._proc)
        return {name: tree_ticks(table, roots) for name, roots in self._groups.items()}

    async def _sample(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            self._samples.append((self._clock(), self._host()))

    async def __aenter__(self) -> Self:
        self._trees_before = self._trees()
        self._own_before = _own_cpu()
        self._pressure_before = self._pressure()
        self._samples = [(self._clock(), self._host())]
        self._task = asyncio.create_task(self._sample())
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if kind is None:
            self._samples.append((self._clock(), self._host()))
            self.result = self._measure(self._trees(), _own_cpu(), self._pressure())

    def _measure(
        self, trees: dict[str, int | None], own: float, pressure: float | None
    ) -> BatchResources:
        (start, first), (end, last) = self._samples[0], self._samples[-1]
        window = end - start
        ticks = last.total - first.total
        timeline = []
        for (_, before), (at, after) in pairwise(self._samples):
            spent = after.total - before.total
            share = (after.busy - before.busy) / spent if spent else 0.0
            timeline.append((round(at - start, 3), round(share, 4), after.runnable))
        used: dict[str, float | None] = {}
        for name, after_ticks in trees.items():
            before_ticks = self._trees_before[name]
            if after_ticks is None or before_ticks is None:
                used[name] = None
            else:
                used[name] = (after_ticks - before_ticks) / TICKS_PER_SECOND
        used["harness"] = round(own - self._own_before, 3)
        waited = None
        if pressure is not None and self._pressure_before is not None and window > 0:
            waited = round((pressure - self._pressure_before) / window, 4)
        return BatchResources(
            window_seconds=round(window, 3),
            cpus=last.cpus,
            sample_seconds=self._interval,
            host_busy_cpu_seconds=(last.busy - first.busy) / TICKS_PER_SECOND,
            host_busy_fraction=round((last.busy - first.busy) / ticks, 4) if ticks else 0.0,
            host_steal_fraction=round((last.steal - first.steal) / ticks, 4) if ticks else 0.0,
            cpu_wait_fraction=waited,
            processes=used,
            timeline=timeline,
        )


class DatabaseCalls(BaseModel):
    """One kind of store call the workers waited on, added up over the whole pool."""

    calls: int
    seconds: float
    max_seconds: float


def combine_calls(reports: Iterable[Mapping[str, Any]]) -> dict[str, DatabaseCalls]:
    """Each kind of store call's count and time, summed over the workers' own reports."""
    calls: dict[str, int] = defaultdict(int)
    seconds: dict[str, float] = defaultdict(float)
    longest: dict[str, float] = defaultdict(float)
    for report in reports:
        for name, stats in report["database"].items():
            calls[name] += stats["calls"]
            seconds[name] += stats["seconds"]
            longest[name] = max(longest[name], stats["max_seconds"])
    return {
        name: DatabaseCalls(
            calls=calls[name], seconds=round(seconds[name], 6), max_seconds=round(longest[name], 6)
        )
        for name in sorted(calls)
    }
