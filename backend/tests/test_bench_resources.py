import asyncio
import sys
from pathlib import Path

import pytest

from bench.resources import (
    TICKS_PER_SECOND,
    DatabaseCalls,
    Sampler,
    combine_calls,
    named,
    parse_pressure,
    parse_process,
    parse_stat,
    processes,
    tree_ticks,
)

_STAT = (
    "cpu  100 5 50 1000 20 3 2 7 40 0\n"
    "cpu0 50 2 25 500 10 1 1 4 20 0\n"
    "cpu1 50 3 25 500 10 2 1 3 20 0\n"
    "intr 12345 0 0\n"
    "ctxt 999\n"
    "procs_running 3\n"
    "procs_blocked 0\n"
)

# utime 7, stime 3, cutime 20 and cstime 5 ticks, under pid 1
_PROCESS = "42 (python3 (x) y) S 1 42 42 0 -1 4194560 100 0 0 0 7 3 20 5 20 0 1 0 5 0 0\n"

# a busy loop that runs until it has used this many seconds of cpu of its own
_BURN = "import time\nstart = time.process_time()\nwhile time.process_time() - start < {}: pass\n"

# waits for a go, then burns cpu in two children: one it waits for, one it leaves running
_PARENT = """
import subprocess, sys
burn = sys.argv[1]
print("ready", flush=True)
sys.stdin.readline()
subprocess.run([sys.executable, "-c", burn], check=True)
alive = subprocess.Popen(
    [sys.executable, "-c", burn + "print('burnt', flush=True)\\nimport time\\ntime.sleep(60)"],
    stdout=subprocess.PIPE,
)
alive.stdout.readline()
print("done", flush=True)
sys.stdin.readline()
alive.kill()
"""


def _write_process(proc: Path, pid: int, parent: int, ticks: int, name: str = "worker") -> None:
    folder = proc / str(pid)
    folder.mkdir(parents=True, exist_ok=True)
    fields = ["S", str(parent), *["0"] * 9, str(ticks), *["0"] * 10]
    (folder / "stat").write_text(f"{pid} ({name}) {' '.join(fields)}\n")
    (folder / "comm").write_text(f"{name}\n")


def test_host_cpu_sums_the_first_eight_columns_leaving_guest_time_inside_user() -> None:
    host = parse_stat(_STAT)
    # busy is user, nice, system, irq and softirq; idle takes iowait; steal stands apart
    assert (host.busy, host.idle, host.steal) == (160, 1020, 7)
    assert host.total == 1187
    assert (host.runnable, host.cpus) == (3, 2)


def test_pressure_reads_the_some_total_in_seconds() -> None:
    text = (
        "some avg10=1.50 avg60=0.20 avg300=0.00 total=2500000\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    )
    assert parse_pressure(text) == 2.5
    with pytest.raises(ValueError):
        parse_pressure("full avg10=0.00 total=1\n")


def test_a_process_line_parses_past_a_command_name_holding_spaces_and_parentheses() -> None:
    assert parse_process(_PROCESS) == (1, 35)


def test_a_tree_counts_its_root_and_every_descendant_once() -> None:
    table = {1: (0, 1000), 10: (1, 5), 11: (10, 7), 12: (11, 11), 20: (1, 13), 21: (20, 17)}
    assert tree_ticks(table, [10]) == 23
    assert tree_ticks(table, [10, 20]) == 53
    # a root under another root is still counted once
    assert tree_ticks(table, [10, 11]) == 23
    assert tree_ticks(table, [10, 99]) is None
    assert tree_ticks(table, []) is None


def test_the_process_table_and_names_come_from_a_proc_folder(tmp_path: Path) -> None:
    _write_process(tmp_path, 7, 1, 30, "dockerd")
    _write_process(tmp_path, 8, 7, 12, "containerd")
    _write_process(tmp_path, 9, 1, 4)
    # a process that exits mid-scan leaves a folder without its files, and is left out
    (tmp_path / "10").mkdir()
    (tmp_path / "self").mkdir()
    assert processes(tmp_path) == {7: (1, 30), 8: (7, 12), 9: (1, 4)}
    assert named(["dockerd", "containerd"], tmp_path) == [7, 8]


async def test_the_sampler_measures_a_fake_host_between_enter_and_exit(tmp_path: Path) -> None:
    (tmp_path / "stat").write_text(_STAT)
    _write_process(tmp_path, 5, 1, 100)
    _write_process(tmp_path, 6, 5, 50)
    times = iter([0.0, 2.0])
    groups = {"workers": [5], "gone": [77]}
    async with Sampler(groups, proc=tmp_path, clock=lambda: next(times)) as sampler:
        # 800 ticks pass: 400 busy, 40 stolen; the worker's child uses 30
        (tmp_path / "stat").write_text(
            _STAT.replace("cpu  100 5 50 1000 20 3 2 7", "cpu  500 5 50 1360 20 3 2 47")
        )
        _write_process(tmp_path, 6, 5, 80)
    used = sampler.result
    assert used is not None
    assert used.window_seconds == 2.0
    assert used.cpus == 2
    assert used.host_busy_cpu_seconds == 400 / TICKS_PER_SECOND
    assert (used.host_busy_fraction, used.host_steal_fraction) == (0.5, 0.05)
    # there is no pressure file to read in the fake folder
    assert used.cpu_wait_fraction is None
    assert used.processes["workers"] == 30 / TICKS_PER_SECOND
    assert used.processes["gone"] is None
    assert used.processes["harness"] is not None
    assert used.timeline == [(2.0, 0.5, 3)]


async def test_the_sampler_leaves_no_result_when_the_batch_failed(tmp_path: Path) -> None:
    (tmp_path / "stat").write_text(_STAT)
    sampler = Sampler({}, proc=tmp_path, interval=0.01)
    with pytest.raises(RuntimeError):
        async with sampler:
            await asyncio.sleep(0.05)
            raise RuntimeError("the batch failed")
    assert sampler.result is None


@pytest.mark.skipif(not Path("/proc/stat").exists(), reason="needs a Linux /proc")
async def test_a_tree_counts_children_it_waited_for_and_children_still_running() -> None:
    parent = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _PARENT,
        _BURN.format(0.3),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    assert parent.stdin is not None and parent.stdout is not None
    try:
        # the window opens once the parent's own start-up is done
        assert await parent.stdout.readline() == b"ready\n"
        async with Sampler({"tree": [parent.pid]}, interval=0.05) as sampler:
            parent.stdin.write(b"go\n")
            await parent.stdin.drain()
            assert await parent.stdout.readline() == b"done\n"
        used = sampler.result
        assert used is not None
        tree = used.processes["tree"]
        assert tree is not None
        # two children burnt 0.3 s each; the one waited for counts through the parent's cutime
        assert 0.55 <= tree <= used.window_seconds * used.cpus
        assert used.host_busy_cpu_seconds >= tree - 0.1
        assert len(used.timeline) >= 3
        assert all(0 <= share <= 1 for _, share, _ in used.timeline)
    finally:
        parent.stdin.write(b"stop\n")
        await parent.stdin.drain()
        await parent.wait()


def test_the_pool_s_store_calls_add_up_over_its_workers_reports() -> None:
    reports = [
        {"database": {"claim": {"calls": 2, "seconds": 0.5, "max_seconds": 0.3}}},
        {
            "database": {
                "claim": {"calls": 3, "seconds": 0.25, "max_seconds": 0.1},
                "publish": {"calls": 1, "seconds": 0.125, "max_seconds": 0.125},
            }
        },
    ]
    assert combine_calls(reports) == {
        "claim": DatabaseCalls(calls=5, seconds=0.75, max_seconds=0.3),
        "publish": DatabaseCalls(calls=1, seconds=0.125, max_seconds=0.125),
    }
    assert combine_calls([]) == {}
