"""Recompute every M20 fault-injection number from the committed chaos records.

    python3 docs/results/chaos/analysis.py

Standard library only. It prints the fault matrix (scenarios against runs, faults injected and
violations), the hung-transaction scenarios before and after their fix, the agent's grading on and
off the worker's event loop, and which deliberately broken builds the harness caught.
"""

import json
import statistics
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _records(folder: Path) -> list[dict]:
    return [json.loads(path.read_text()) for path in sorted(folder.glob("*/seed-*.json"))]


def _violations(record: dict) -> int:
    return sum(len(found) for found in record["violations"].values())


def _build(records: list[dict], folder: Path | None = None) -> str:
    shas = ", ".join(sorted({record["build"]["git_sha"][:7] for record in records}))
    if folder is not None and (folder / "mutation.diff").exists():
        # a broken build is its commit with one change on top, kept beside its records
        return f"build {shas} plus {folder.name}/mutation.diff"
    dirty = any(record["build"]["git_dirty"] for record in records)
    return f"build {shas}, {'dirty' if dirty else 'clean'} checkout"


def matrix() -> None:
    records = _records(HERE / "m20")
    order = list(dict.fromkeys(record["scenario"] for record in records))
    print(f"fault matrix: {len(records)} runs, {_build(records)}")
    print(
        f"  {'scenario':38} {'point':24} {'fault':7} {'runs':>4} {'faults':>6} {'violations':>10}"
    )
    for name in order:
        runs = [record for record in records if record["scenario"] == name]
        first = runs[0]
        print(
            f"  {name:38} {first['point']:24} {first['action']:7} {len(runs):4} "
            f"{sum(record['faults_injected'] for record in runs):6} "
            f"{sum(_violations(record) for record in runs):10}"
        )
    own = Counter(
        fault["action"]
        for record in records
        for fault in record["faults"]
        if fault["point"] == record["point"]
    )
    planned = sum(record["config"]["faults_planned"] for record in records)
    injected = sum(record["faults_injected"] for record in records)
    violations = sum(map(_violations, records))
    print(f"  all: {injected} of {planned} planned faults injected, {violations} violations")
    print(f"  by fault: {dict(sorted(own.items()))}")
    print(f"  failed runs: {[r['scenario'] for r in records if not r['passed']] or 'none'}")
    counts = Counter()
    for record in records:
        counts.update(
            {key: value for key, value in record["counts"].items() if isinstance(value, int)}
        )
    print(
        f"  jobs run more than once: {counts['jobs_run_more_than_once']}, worker and api processes "
        f"started: {counts['processes_started']}"
    )
    walls = [record["wall_seconds"] for record in records]
    print(f"  wall per run: median {statistics.median(walls):.1f} s, max {max(walls):.1f} s")


def hangs() -> None:
    names = {"claim-before-commit-hang", "publish-before-commit-hang"}
    sides = [
        (
            "found, before the fix",
            HERE / "m20-before-idle-timeout",
            _records(HERE / "m20-before-idle-timeout"),
        ),
        (
            "the fix undone on head",
            HERE / "m20-mutations" / "sessions-never-time-out",
            _records(HERE / "m20-mutations" / "sessions-never-time-out"),
        ),
        (
            "with the fix, the matrix",
            None,
            [r for r in _records(HERE / "m20") if r["scenario"] in names],
        ),
    ]
    print("a worker hung mid-transaction, the claim and publish hang scenarios:")
    for label, folder, runs in sides:
        print(
            f"  {label:26} {len(runs)} runs, {sum(r['passed'] for r in runs)} passed, "
            f"{sum(r['drained'] for r in runs)} drained, {sum(map(_violations, runs))} violations "
            f"({_build(runs, folder)})"
        )


def grading() -> None:
    print("the agent's grading on and off the worker's event loop, two cpus, ten seeds each:")
    for side in ("on-the-loop", "off-the-loop"):
        folder = HERE / "m20-agent-grading" / side
        runs = _records(folder)
        late = sum(record["counts"]["late_results_refused"] for record in runs)
        lost = sum(record["counts"]["leases_lost"] for record in runs)
        rerun = sum(record["counts"]["jobs_run_more_than_once"] for record in runs)
        with_late = sum(record["counts"]["late_results_refused"] > 0 for record in runs)
        print(
            f"  {side:13} {len(runs)} runs, {sum(r['passed'] for r in runs)} passed; "
            f"{late} late results refused (in {with_late} runs), {lost} leases lost, "
            f"{rerun} jobs run more than once ({_build(runs, folder)})"
        )


def mutations() -> None:
    print("deliberately broken builds, each its commit plus the one change in its mutation.diff:")
    for root in (HERE / "m20-mutations", HERE / "m20-mutations-first-try"):
        for folder in sorted(path for path in root.iterdir() if path.is_dir()):
            what = json.loads((folder / "mutation.json").read_text())
            runs = _records(folder)
            caught = sum(not record["passed"] for record in runs)
            found = sum(_violations(record) for record in runs)
            kinds = Counter(
                kind
                for record in runs
                for kind, problems in record["violations"].items()
                if problems
            )
            label = f"{folder.name}{' (first try)' if root.name.endswith('first-try') else ''}"
            print(
                f"  {label:38} caught in {caught} of {len(runs)} runs of {what['scenarios']}, "
                f"{found} violations, from {dict(sorted(kinds.items()))} ({_build(runs, folder)})"
            )
            print(f"  {'':38} {what['why']}")


def main() -> None:
    matrix()
    print()
    hangs()
    print()
    grading()
    print()
    mutations()


if __name__ == "__main__":
    main()
