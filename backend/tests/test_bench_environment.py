import platform
import re
import subprocess
from pathlib import Path

from bench.environment import (
    capture_environment,
    cpu_quota_cores,
    git_dirty,
    memory_limit_bytes,
    parse_cpu_model,
    parse_mem_total_gib,
    parse_virtualized,
)

_CPUINFO = (
    "processor\t: 0\n"
    "model name\t: Intel(R) Xeon(R) Processor @ 2.80GHz\n"
    "flags\t\t: fpu vme sse2 hypervisor avx2\n"
)


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_cpuinfo_parsing() -> None:
    assert parse_cpu_model(_CPUINFO) == "Intel(R) Xeon(R) Processor @ 2.80GHz"
    assert parse_virtualized(_CPUINFO)
    assert not parse_virtualized(_CPUINFO.replace("hypervisor ", ""))


def test_meminfo_parsing() -> None:
    assert parse_mem_total_gib("MemTotal:       16777216 kB\nMemFree: 1 kB\n") == 16.0


def test_cgroup_v2_limits(tmp_path: Path) -> None:
    _write(tmp_path, "cpu.max", "200000 100000")
    _write(tmp_path, "memory.max", "1073741824")
    assert cpu_quota_cores(tmp_path) == 2.0
    assert memory_limit_bytes(tmp_path) == 1073741824


def test_cgroup_v2_unlimited(tmp_path: Path) -> None:
    _write(tmp_path, "cpu.max", "max 100000")
    _write(tmp_path, "memory.max", "max")
    assert cpu_quota_cores(tmp_path) is None
    assert memory_limit_bytes(tmp_path) is None


def test_cgroup_v1_limits(tmp_path: Path) -> None:
    _write(tmp_path, "cpu/cpu.cfs_quota_us", "50000")
    _write(tmp_path, "cpu/cpu.cfs_period_us", "100000")
    _write(tmp_path, "memory/memory.limit_in_bytes", "2147483648")
    assert cpu_quota_cores(tmp_path) == 0.5
    assert memory_limit_bytes(tmp_path) == 2147483648


def test_cgroup_v1_unlimited_and_missing(tmp_path: Path) -> None:
    assert cpu_quota_cores(tmp_path) is None
    assert memory_limit_bytes(tmp_path) is None
    _write(tmp_path, "cpu/cpu.cfs_quota_us", "-1")
    _write(tmp_path, "cpu/cpu.cfs_period_us", "100000")
    assert cpu_quota_cores(tmp_path) is None


def test_git_dirty_ignores_the_bench_output_records(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    _write(tmp_path, "code.py", "x = 1\n")
    git("add", ".")
    git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "c",
    )
    assert not git_dirty(tmp_path)
    _write(tmp_path, "docs/results/bench/demo/trial-1.json", "{}")
    assert not git_dirty(tmp_path)
    _write(tmp_path, "code.py", "x = 2\n")
    assert git_dirty(tmp_path)


def test_capture_on_this_machine() -> None:
    env = capture_environment()
    assert re.fullmatch(r"[0-9a-f]{40}", env.git_sha)
    assert 1 <= env.usable_cpus <= env.logical_cpus
    assert env.memory_total_gib > 0
    assert env.python == platform.python_version()
    assert env.kernel == platform.release()
    assert isinstance(env.sandbox_network_isolated, bool)
