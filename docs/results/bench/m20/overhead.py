"""Did M20 cost anything? Three comparisons, all recomputed from the committed records.

    python3 docs/results/bench/m20/overhead.py

Standard library only. First, the one-worker a/b run by the M19 and M20 code alternately: for each
build, the batch wall clock of both arms, the fleet's overhead over sequential per trial pair,
worker utilization, the idle gap between one job's publish and the next claim, the wait before the
first claim, and job service time. Then two sequential-replay follow-ups: three fresh worktrees side
by side (M19, M20, and M20 with only its grading change undone), and the same code from the main
checkout against a fresh worktree.
"""

import json
import statistics
from itertools import pairwise
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILDS = HERE / "builds"
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


def _graded(job: dict) -> bool:
    # the two escalation tickets never reach their hidden tests
    return not job["task_id"].startswith("TCK")


def _sequential(folder: Path, arms: list[str]) -> None:
    per: dict[tuple[str, str], list[float]] = {}
    for arm in arms:
        trials = [
            json.loads(path.read_text())
            for path in sorted((folder / arm).glob("round-*/trial-*.json"))
        ]
        walls = [trial["metrics"]["batch_wall_clock_seconds"] for trial in trials]
        shas = ", ".join(sorted({trial["environment"]["git_sha"][:7] for trial in trials}))
        dirty = any(trial["environment"]["git_dirty"] for trial in trials)
        graded = [
            job["finished_at"] - job["claimed_at"]
            for trial in trials
            for job in trial["jobs"]
            if _graded(job)
        ]
        print(
            f"  {arm:22} {len(trials)} trials, batch wall clock {_spread(walls)}, "
            f"graded-task service median {statistics.median(graded) * 1000:.1f} ms "
            f"(build {shas}{', plus the diff beside it' if dirty else ', clean'})"
        )
        for trial in trials:
            for job in trial["jobs"]:
                per.setdefault((arm, job["task_id"]), []).append(
                    job["finished_at"] - job["claimed_at"]
                )
    _per_task(per, arms)


def _per_task(per: dict[tuple[str, str], list[float]], arms: list[str]) -> None:
    """Each task's median service time in one arm against another, for every pair of arms."""
    tasks = sorted({task for _, task in per if not task.startswith("TCK")})
    for index, arm in enumerate(arms):
        for base in arms[:index]:
            diffs = [
                statistics.median(per[(arm, task)]) - statistics.median(per[(base, task)])
                for task in tasks
            ]
            print(
                f"  {arm} minus {base}, per graded task: "
                f"{_spread(diffs, 'ms', 1000, 1, sign=True)}, "
                f"slower in {sum(diff > 0 for diff in diffs)} of {len(diffs)}"
            )


def main() -> None:
    for build in ("m19", "m20"):
        _report(build)
    per: dict[tuple[str, str], list[float]] = {}
    for build in ("m19", "m20"):
        for pair in _pairs(build):
            for job in pair[0]["jobs"]:
                per.setdefault((build, job["task_id"]), []).append(
                    job["finished_at"] - job["claimed_at"]
                )
    print("the sequential arm, task by task:")
    _per_task(per, ["m19", "m20"])
    print("sequential replay from three fresh worktrees side by side, order rotating:")
    _sequential(HERE / "three-arms", ["m19", "m20", "m20-grading-on-loop"])
    print("the same code from the main checkout and from a fresh worktree, alternating:")
    _sequential(HERE / "checkout", ["worktree", "main"])


if __name__ == "__main__":
    main()
