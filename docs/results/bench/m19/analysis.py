"""Recompute every M19 number from the committed records.

    python3 docs/results/bench/m19/analysis.py

Standard library only. It prints the in-process against in-container A/B and where a container's
extra time per job goes, what the bench's tasks used against their default limits, the overhead
probes' timings, and how long cancels took.
"""

import json
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
# each a/b's two arms, at zero latency and at the model's recorded latency
ARMS = {
    latency: (f"ab-fleet-1w-replay-{latency}", f"ab-fleet-1w-container-replay-{latency}")
    for latency in ("zero", "recorded")
}


def _trials(name: str) -> list[dict]:
    return [json.loads(path.read_text()) for path in sorted((HERE / name).glob("trial-*.json"))]


def _spread(
    values: list[float], unit: str = "s", scale: float = 1, digits: int = 2, sign: bool = False
) -> str:
    spec = f"{'+' if sign else ''}.{digits}f"
    low, mid, high = (
        format(scale * value, spec)
        for value in (min(values), statistics.median(values), max(values))
    )
    return f"{mid}{' ' + unit if unit else ''} [{low} to {high}]"


def _service(job: dict) -> float:
    return job["finished_at"] - job["claimed_at"]


def _ab(latency: str, process: list[dict], container: list[dict]) -> None:
    records = process + container
    shas = ", ".join(sorted({record["environment"]["git_sha"][:7] for record in records}))
    clean = not any(record["environment"]["git_dirty"] for record in records)
    wall = {
        name: [trial["metrics"]["batch_wall_clock_seconds"] for trial in trials]
        for name, trials in (("process", process), ("container", container))
    }
    pairs = list(zip(wall["process"], wall["container"], strict=True))
    same = all(
        {job["job_id"]: job["outcome"] for job in a["jobs"]}
        == {job["job_id"]: job["outcome"] for job in b["jobs"]}
        for a, b in zip(process, container, strict=True)
    )
    print(f"a/b at {latency} latency: {len(pairs)} trial pairs, build {shas}, clean: {clean}")
    print(f"  batch wall clock, in process    {_spread(wall['process'])}")
    print(f"  batch wall clock, in containers {_spread(wall['container'])}")
    print(f"  containers minus process        {_spread([c - p for p, c in pairs], sign=True)}")
    print(f"  containers over process         {_spread([c / p for p, c in pairs], 'x')}")
    print(f"  per-job outcomes identical in every trial pair: {same}")
    process_jobs = [job for trial in process for job in trial["jobs"]]
    container_jobs = [job for trial in container for job in trial["jobs"]]
    inside = [job["execution"]["seconds"] for job in container_jobs]
    outside = [_service(job) - job["execution"]["seconds"] for job in container_jobs]
    print(f"  job service, in process         {_spread([_service(j) for j in process_jobs])}")
    print(f"  job service, in a container     {_spread([_service(j) for j in container_jobs])}")
    print(f"    of which, container running   {_spread(inside)}")
    print(f"    of which, around it           {_spread(outside, 'ms', 1000, 0)}")
    tasks = sorted({job["task_id"] for job in process_jobs})
    extra = [
        statistics.median(_service(j) for j in container_jobs if j["task_id"] == task)
        - statistics.median(_service(j) for j in process_jobs if j["task_id"] == task)
        for task in tasks
    ]
    print(f"  extra service per task (median) {_spread(extra, sign=True)} over {len(tasks)} tasks")


def _usage(container: list[dict]) -> None:
    jobs = [job for trial in container for job in trial["jobs"]]
    policy = jobs[0]["execution"]["policy"]
    usage = [job["execution"]["usage"] for job in jobs]
    own = [item["max_rss_mb"] for item in usage]
    child = [item["max_child_rss_mb"] for item in usage]
    cpu = [item["cpu_seconds"] for item in usage]
    busy = [job["execution"]["usage"]["cpu_seconds"] / job["execution"]["seconds"] for job in jobs]
    stdout = [job["execution"]["stdout_bytes"] for job in jobs]
    print(f"usage: {len(jobs)} container jobs, policy {policy}")
    print(f"  peak rss, task process          {_spread(own, 'MB', digits=1)}")
    print(f"  peak rss, largest child         {_spread(child, 'MB', digits=1)}")
    print(f"  cpu seconds per job             {_spread(cpu)}")
    print(f"  cpu seconds per container second {_spread(busy, '', digits=2)}")
    print(f"  stdout bytes per job            {_spread(stdout, 'KB', 1 / 1024, 1)}")
    ended = [job["execution"] for job in jobs]
    exits = sorted({(item["exit_code"], item["oom_killed"]) for item in ended})
    print(f"  (exit code, oom killed) seen    {exits}")


def _overhead() -> None:
    record = json.loads((HERE / "container-overhead.json").read_text())
    config = record["config"]
    print(
        f"overhead probes: {config['rounds']} rounds x {config['repeat_per_round']} repeats, "
        f"build {record['environment']['git_sha'][:7]}, "
        f"clean checkout: {not record['environment']['git_dirty']}"
    )
    names = sorted({name for summary in record["summary"].values() for name in summary})
    for name in names:
        values = "  ".join(
            f"{mode} {summary[name] * 1000:8.1f} ms" if name in summary else f"{mode} {'-':>11}"
            for mode, summary in record["summary"].items()
        )
        print(f"  {name:36} {values}")


def _cancellation(mode: str) -> None:
    record = json.loads((HERE / f"cancellation-{mode}.json").read_text())
    config, checks = record["config"], record["checks"]
    running = [item["running"] for item in record["rounds"]]
    queued = [item["queued"] for item in record["rounds"]]
    print(
        f"cancellation, {mode}: {config['rounds']} rounds, lease {config['lease_seconds']} s, "
        f"heartbeat {config['heartbeat_seconds']} s, build {record['environment']['git_sha'][:7]}, "
        f"clean checkout: {not record['environment']['git_dirty']}"
    )
    print(f"  checks                          {checks}")
    latency = [item["latency_seconds"] for item in running]
    waiting = [item["until_heartbeat_seconds"] for item in running]
    stopping = [item["stop_seconds"] for item in running]
    print(f"  running: request to end         {_spread(latency)}")
    print(f"  running: waiting for heartbeat  {_spread(waiting)}")
    print(f"  running: stopping after it      {_spread(stopping, 'ms', 1000, 0)}")
    print(
        f"  queued: api round trip          "
        f"{_spread([q['api_round_trip_seconds'] for q in queued], 'ms', 1000, 1)}"
    )


def main() -> None:
    for latency, (process_arm, container_arm) in ARMS.items():
        process, container = _trials(process_arm), _trials(container_arm)
        _ab(latency, process, container)
        print()
        _usage(container)
        print()
    _overhead()
    for mode in ("container", "process"):
        print()
        _cancellation(mode)


if __name__ == "__main__":
    main()
