"""Capture the code and machine a run happened on, so every number carries its setup."""

import os
import platform
import subprocess
from pathlib import Path

from pydantic import BaseModel

from app.sandbox.subprocess_sandbox import SubprocessSandbox

REPO_ROOT = Path(__file__).resolve().parents[2]
_GIB = 1024**3


class Environment(BaseModel):
    git_sha: str
    git_dirty: bool
    cpu_model: str
    logical_cpus: int
    usable_cpus: int
    # cgroup limits, None when the process is not limited
    cpu_quota_cores: float | None
    memory_total_gib: float
    memory_limit_gib: float | None
    virtualized: bool
    os: str
    kernel: str
    python: str
    # whether sandboxed commands got a private network namespace; None in records made before
    sandbox_network_isolated: bool | None = None


def sandbox_network_isolated() -> bool:
    sandbox = SubprocessSandbox()
    try:
        return sandbox.network_isolated
    finally:
        sandbox.cleanup()


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _field(text: str, name: str) -> str | None:
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == name:
            return value.strip()
    return None


def parse_cpu_model(cpuinfo: str) -> str:
    return _field(cpuinfo, "model name") or platform.processor() or "unknown"


def parse_virtualized(cpuinfo: str) -> bool:
    """True when the cpu flags report a hypervisor underneath."""
    return "hypervisor" in (_field(cpuinfo, "flags") or "").split()


def parse_mem_total_gib(meminfo: str) -> float:
    total_kib = (_field(meminfo, "MemTotal") or "0 kB").split()[0]
    return round(int(total_kib) * 1024 / _GIB, 2)


def cpu_quota_cores(cgroup_root: Path) -> float | None:
    """The cgroup cpu limit in cores (v2 cpu.max, else v1 cfs quota), or None if unlimited."""
    v2 = _read(cgroup_root / "cpu.max")
    if v2 is not None:
        limit, _, window = v2.partition(" ")
        return None if limit == "max" else int(limit) / int(window)
    quota = _read(cgroup_root / "cpu" / "cpu.cfs_quota_us")
    period = _read(cgroup_root / "cpu" / "cpu.cfs_period_us")
    if quota is None or period is None or int(quota) < 0:
        return None
    return int(quota) / int(period)


def memory_limit_bytes(cgroup_root: Path) -> int | None:
    """The cgroup memory limit (v2 memory.max, else v1 limit_in_bytes), or None if unlimited."""
    raw = _read(cgroup_root / "memory.max") or _read(
        cgroup_root / "memory" / "memory.limit_in_bytes"
    )
    if raw is None or raw == "max":
        return None
    return int(raw)


def _git(repo: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, timeout=10, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip()


def git_dirty(repo: Path) -> bool:
    """True when anything but the bench's own output records differs from the commit."""
    # a batch writes each trial's record before the next trial starts, so outputs are excluded
    return bool(_git(repo, "status", "--porcelain", "--", ".", ":(exclude)docs/results/bench"))


def _os_name(os_release: str) -> str:
    pretty = _field(os_release.replace("=", ":"), "PRETTY_NAME")
    return pretty.strip('"') if pretty else platform.platform()


def capture_environment(
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    os_release: Path = Path("/etc/os-release"),
) -> Environment:
    cpuinfo = _read(proc_root / "cpuinfo") or ""
    memory_total_gib = parse_mem_total_gib(_read(proc_root / "meminfo") or "")
    limit = memory_limit_bytes(cgroup_root)
    # a limit at or above physical memory does not constrain anything
    limit_gib = round(limit / _GIB, 2) if limit is not None else None
    if limit_gib is not None and limit_gib >= memory_total_gib:
        limit_gib = None
    logical = os.cpu_count() or 1
    return Environment(
        git_sha=_git(REPO_ROOT, "rev-parse", "HEAD") or "unknown",
        git_dirty=git_dirty(REPO_ROOT),
        cpu_model=parse_cpu_model(cpuinfo),
        logical_cpus=logical,
        usable_cpus=len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else logical,
        cpu_quota_cores=cpu_quota_cores(cgroup_root),
        memory_total_gib=memory_total_gib,
        memory_limit_gib=limit_gib,
        virtualized=parse_virtualized(cpuinfo),
        os=_os_name(_read(os_release) or ""),
        kernel=platform.release(),
        python=platform.python_version(),
        sandbox_network_isolated=sandbox_network_isolated(),
    )
