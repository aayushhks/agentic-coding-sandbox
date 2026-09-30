"""Recompute the fleet's per-job overhead from committed bench trial records.

    python3 docs/results/bench/m18/overhead.py

Standard library only. For each one-worker comparison it prints the batch wall clock of both arms,
the fleet's overhead over sequential per trial pair, worker utilization, the idle gap between one
job's publish and the next claim, the wait before the first claim, and job service time.
"""

import json
import statistics
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
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


def _pairs(roots: list[Path]) -> list[tuple[dict, dict]]:
    return [
        pair
        for root in roots
        for pair in zip(_trials(root / SEQUENTIAL), _trials(root / FLEET), strict=True)
    ]


def _report(name: str, pairs: list[tuple[dict, dict]]) -> None:
    median = statistics.median
    seq_wall = [seq["metrics"]["batch_wall_clock_seconds"] for seq, _ in pairs]
    fleet_wall = [fleet["metrics"]["batch_wall_clock_seconds"] for _, fleet in pairs]
    overhead = [f - s for s, f in zip(seq_wall, fleet_wall, strict=True)]
    gaps, firsts, fleet_service, seq_service = [], [], [], []
    for seq, fleet in pairs:
        jobs = sorted(fleet["jobs"], key=lambda job: job["claimed_at"])
        firsts.append(jobs[0]["claimed_at"] - jobs[0]["submitted_at"])
        gaps += [later["claimed_at"] - earlier["finished_at"] for earlier, later in zip(jobs, jobs[1:])]
        fleet_service += [job["finished_at"] - job["claimed_at"] for job in jobs]
        seq_service += [job["finished_at"] - job["claimed_at"] for job in seq["jobs"]]
    shas = sorted({record["environment"]["git_sha"][:7] for pair in pairs for record in pair})
    clean = not any(record["environment"]["git_dirty"] for pair in pairs for record in pair)
    utilization = [fleet["metrics"]["utilization"] for _, fleet in pairs]
    print(f"{name}: {len(pairs)} trial pairs, build {', '.join(shas)}, clean checkout: {clean}")
    print(f"  batch wall clock, sequential  {median(seq_wall):.2f} s [{min(seq_wall):.2f}-{max(seq_wall):.2f}]")
    print(f"  batch wall clock, fleet       {median(fleet_wall):.2f} s [{min(fleet_wall):.2f}-{max(fleet_wall):.2f}]")
    print(
        f"  fleet minus sequential        {median(overhead):+.2f} s "
        f"[{min(overhead):+.2f} to {max(overhead):+.2f}]"
    )
    print(f"  fleet utilization             {median(utilization):.3f} [{min(utilization):.3f}-{max(utilization):.3f}]")
    print(
        f"  publish-to-next-claim gap     {median(gaps) * 1000:.2f} ms "
        f"(p95 {_percentile(gaps, 95) * 1000:.2f}, max {max(gaps) * 1000:.2f}, n={len(gaps)})"
    )
    print(
        f"  first claim after submit      {median(firsts) * 1000:.1f} ms "
        f"[{min(firsts) * 1000:.1f}-{max(firsts) * 1000:.1f}]"
    )
    print(
        f"  service time median           fleet {median(fleet_service) * 1000:.1f} ms, "
        f"sequential {median(seq_service) * 1000:.1f} ms"
    )


def main() -> None:
    _report("M17 a/b (its own session)", _pairs([BENCH]))
    _report("M18 a/b (its own session)", _pairs([BENCH / "m18"]))
    builds = BENCH / "m18" / "builds"
    for build in ("m17", "m18"):
        runs = sorted((builds / build).glob("run-*"), key=lambda path: int(path.name.split("-")[1]))
        _report(f"build against build, {build} code (one session, interleaved)", _pairs(runs))


if __name__ == "__main__":
    main()
