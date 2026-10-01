"""Did M20 cost anything? The one-worker a/b, run by the M19 and M20 code alternately.

    python3 docs/results/bench/m20/overhead.py

Standard library only. For each build it prints the batch wall clock of both arms, the fleet's
overhead over sequential per trial pair, worker utilization, the idle gap between one job's publish
and the next claim, the wait before the first claim, and job service time.
"""

import json
import statistics
from itertools import pairwise
from pathlib import Path

BUILDS = Path(__file__).resolve().parent / "builds"
SEQUENTIAL = "ab-sequential-replay-zero"
FLEET = "ab-fleet-1w-replay-zero"


def _percentile(values: list[float], pct: float) -> float:
    # linear interpolation between closest ranks, the bench's own method
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _trials(folder: Path) -> list[dict]:
    return [json.loads(path.read_text()) for path in sorted(folder.glob("trial-*.json"))]


def _pairs(build: str) -> list[tuple[dict, dict]]:
    runs = sorted((BUILDS / build).glob("run-*"), key=lambda path: int(path.name.split("-")[1]))
    return [
        pair
        for run in runs
        for pair in zip(_trials(run / SEQUENTIAL), _trials(run / FLEET), strict=True)
    ]


def _spread(
    values: list[float], unit: str = "s", scale: float = 1, digits: int = 2, sign: bool = False
) -> str:
    spec = f"{'+' if sign else ''}.{digits}f"
    low, mid, high = (
        format(scale * value, spec)
        for value in (min(values), statistics.median(values), max(values))
    )
    return f"{mid}{' ' + unit if unit else ''} [{low} to {high}]"


def _report(build: str) -> None:
    pairs = _pairs(build)
    seq_wall = [seq["metrics"]["batch_wall_clock_seconds"] for seq, _ in pairs]
    fleet_wall = [fleet["metrics"]["batch_wall_clock_seconds"] for _, fleet in pairs]
    overhead = [f - s for s, f in zip(seq_wall, fleet_wall, strict=True)]
    utilization = [fleet["metrics"]["utilization"] for _, fleet in pairs]
    gaps, firsts, fleet_service, seq_service = [], [], [], []
    for seq, fleet in pairs:
        jobs = sorted(fleet["jobs"], key=lambda job: job["claimed_at"])
        firsts.append(jobs[0]["claimed_at"] - jobs[0]["submitted_at"])
        gaps += [later["claimed_at"] - earlier["finished_at"] for earlier, later in pairwise(jobs)]
        fleet_service += [job["finished_at"] - job["claimed_at"] for job in jobs]
        seq_service += [job["finished_at"] - job["claimed_at"] for job in seq["jobs"]]
    records = [record for pair in pairs for record in pair]
    shas = ", ".join(sorted({record["environment"]["git_sha"][:7] for record in records}))
    clean = not any(record["environment"]["git_dirty"] for record in records)
    same = all(
        {job["job_id"]: job["outcome"] for job in seq["jobs"]}
        == {job["job_id"]: job["outcome"] for job in fleet["jobs"]}
        for seq, fleet in pairs
    )
    median = statistics.median
    print(f"{build} code: {len(pairs)} trial pairs, build {shas}, clean checkout: {clean}")
    print(f"  batch wall clock, sequential  {_spread(seq_wall)}")
    print(f"  batch wall clock, fleet       {_spread(fleet_wall)}")
    print(f"  fleet minus sequential        {_spread(overhead, sign=True)}")
    print(f"  fleet utilization             {_spread(utilization, unit='', digits=3)}")
    print(
        f"  publish-to-next-claim gap     {_spread(gaps, 'ms', 1000)}, "
        f"p95 {_percentile(gaps, 95) * 1000:.2f} ms, n={len(gaps)}"
    )
    print(f"  first claim after submit      {_spread(firsts, 'ms', 1000, 1)}")
    print(
        f"  service time median           fleet {median(fleet_service) * 1000:.1f} ms, "
        f"sequential {median(seq_service) * 1000:.1f} ms"
    )
    print(f"  outcomes identical across arms in every pair: {same}")


def main() -> None:
    for build in ("m19", "m20"):
        _report(build)


if __name__ == "__main__":
    main()
