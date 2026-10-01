"""Run fault scenarios against the fleet, and report what was injected and what the checks found.

    uv run python -m chaos run --scenarios all --seeds 0-2 --out ../docs/results/chaos
    uv run python -m chaos summarize ../docs/results/chaos

A run fails, and the command exits 1, on any invariant violation, any job that published the wrong
key or outcome, any fault that didn't leave its effects behind, or any planned fault that never
happened. A throwaway local Postgres is used unless --database-url is given; the Postgres restart
scenario needs one it owns, and is skipped without.
"""

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from bench.environment import capture_environment
from chaos.harness import run_scenario
from chaos.scenarios import BY_NAME, Scenario
from fleet.localdb import LocalPostgres
from fleet.migrate import migrate


def _seeds(spec: str) -> list[int]:
    """ "0-2" or "0,3,7"."""
    if "-" in spec:
        first, last = spec.split("-", 1)
        return list(range(int(first), int(last) + 1))
    return [int(seed) for seed in spec.split(",")]


def _violations(record: dict[str, Any]) -> int:
    return sum(len(found) for found in record["violations"].values())


def _line(record: dict[str, Any]) -> str:
    verdict = "ok" if record["passed"] else "FAILED"
    return (
        f"{record['scenario']:40} seed {record['seed']:<3} {record['faults_injected']} faults  "
        f"{_violations(record)} violations  {record['wall_seconds']:6.1f}s  {verdict}"
    )


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Scenarios against trials, faults injected and violations found, the deliverable table."""
    rows: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"trials": 0, "faults_injected": 0, "violations": 0, "failed_seeds": []}
    )
    for record in records:
        row = rows[record["scenario"]]
        row.update(point=record["point"], action=record["action"], summary=record["summary"])
        row["trials"] += 1
        row["faults_injected"] += record["faults_injected"]
        row["violations"] += _violations(record)
        if not record["passed"]:
            row["failed_seeds"].append(record["seed"])
    ordered = [name for name in BY_NAME if name in rows]
    return {
        "scenarios": {name: rows[name] for name in ordered},
        "trials": sum(row["trials"] for row in rows.values()),
        "faults_injected": sum(row["faults_injected"] for row in rows.values()),
        "violations": sum(row["violations"] for row in rows.values()),
        "failed_runs": sum(len(row["failed_seeds"]) for row in rows.values()),
    }


def _print_table(summary: dict[str, Any]) -> None:
    print(f"\n{'scenario':40} {'trials':>6} {'faults':>7} {'violations':>10}  failed seeds")
    for name, row in summary["scenarios"].items():
        failed = ", ".join(str(seed) for seed in row["failed_seeds"]) or "-"
        counts = f"{row['trials']:6} {row['faults_injected']:7} {row['violations']:10}"
        print(f"{name:40} {counts}  {failed}")
    print(
        f"{'all':40} {summary['trials']:6} {summary['faults_injected']:7} "
        f"{summary['violations']:10}  {summary['failed_runs']} failed runs"
    )


async def _run(
    scenarios: list[Scenario],
    seeds: list[int],
    *,
    url: str,
    cluster: LocalPostgres | None,
    out: Path | None,
    image: str,
    keep: bool,
) -> list[dict[str, Any]]:
    records = []
    for scenario in scenarios:
        if scenario.point == "postgres.restart" and cluster is None:
            print(f"{scenario.name:40} skipped: it restarts a Postgres this run doesn't own")
            continue
        for seed in seeds:
            workdir = Path(tempfile.mkdtemp(prefix=f"chaos-{scenario.name}-{seed}-"))
            record = await run_scenario(
                scenario, seed, url=url, cluster=cluster, workdir=workdir, image=image
            )
            print(_line(record), flush=True)
            if not record["passed"]:
                for kind, found in record["violations"].items():
                    for problem in found[:5]:
                        print(f"    {kind}: {problem}")
                print(f"    logs kept in {workdir}")
            elif not keep:
                shutil.rmtree(workdir, ignore_errors=True)
            if out is not None:
                path = out / scenario.name / f"seed-{seed}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(record, indent=2) + "\n")
            records.append(record)
    return records


def _load(out: Path) -> list[dict[str, Any]]:
    return [json.loads(path.read_text()) for path in sorted(out.glob("*/seed-*.json"))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    running = commands.add_parser("run", help="run scenarios across seeds")
    running.add_argument("--scenarios", default="all", help="all, or names separated by commas")
    running.add_argument("--seeds", default="0-2", help='"0-2" or "0,3,7"')
    running.add_argument("--out", type=Path, default=None, help="where to write each run's record")
    running.add_argument("--database-url", default=None)
    running.add_argument("--task-image", default="fleet-task:local")
    running.add_argument("--execution", choices=["all", "process", "container"], default="all")
    running.add_argument("--keep-logs", action="store_true")
    summary = commands.add_parser("summarize", help="rebuild the table from written records")
    summary.add_argument("out", type=Path)
    args = parser.parse_args(argv)

    if args.command == "summarize":
        table = summarize(_load(args.out))
        _print_table(table)
        return 1 if table["failed_runs"] else 0

    names = list(BY_NAME) if args.scenarios == "all" else args.scenarios.split(",")
    unknown = [name for name in names if name not in BY_NAME]
    if unknown:
        parser.error(f"no scenario {', '.join(unknown)}; there are: {', '.join(BY_NAME)}")
    scenarios = [
        BY_NAME[name] for name in names if args.execution in ("all", BY_NAME[name].execution)
    ]
    cluster = None if args.database_url else LocalPostgres.start()
    url: str = args.database_url or (cluster.url if cluster else "")
    try:
        migrate(url)
        records = asyncio.run(
            _run(
                scenarios,
                _seeds(args.seeds),
                url=url,
                cluster=cluster,
                out=args.out,
                image=args.task_image,
                keep=args.keep_logs,
            )
        )
    finally:
        if cluster is not None:
            cluster.stop()
    table = summarize(records)
    _print_table(table)
    if args.out is not None:
        table["environment"] = capture_environment().model_dump(mode="json")
        (args.out / "summary.json").write_text(json.dumps(table, indent=2) + "\n")
    return 1 if table["failed_runs"] else 0


if __name__ == "__main__":
    sys.exit(main())
