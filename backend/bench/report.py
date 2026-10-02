"""The platform report: every number on the published page, computed from the committed records.

    python -m bench.report      # rewrites frontend/public/platform-report.json

The page renders this file and nothing else, and a test regenerates it from the records and fails
on any difference, so the numbers on the page and the records they come from cannot drift apart.
Every section names the configuration its numbers were measured under and the records behind them.
"""

import argparse
import json
import statistics
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from bench.environment import REPO_ROOT

REPORT_PATH = REPO_ROOT / "frontend" / "public" / "platform-report.json"
REPOSITORY = "https://github.com/aayushhks/agentic-coding-sandbox"
RESULTS = REPO_ROOT / "docs" / "results"

median = statistics.median
Table = dict[str, Any]
# typographic marks for the page, spelled out so they can't be mistaken for their ascii lookalikes
DASH = "\u2013"
TIMES = "\u00d7"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _relative(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def _trials(folder: Path) -> list[dict[str, Any]]:
    trials = [_load(path) for path in folder.glob("trial-*.json")]
    return sorted(trials, key=lambda trial: trial["trial"])


def _pools(run: Path) -> dict[int, list[dict[str, Any]]]:
    """Each pool's trials, by worker count."""
    pools: dict[int, list[dict[str, Any]]] = {}
    for folder in sorted(path for path in run.iterdir() if path.is_dir()):
        trials = _trials(folder)
        if trials:
            pools[trials[0]["config"]["workers"]] = trials
    return dict(sorted(pools.items()))


def _spread(values: Iterable[float], digits: int = 1) -> str:
    """The median and the range; a single value has no range to show."""
    ordered = sorted(values)
    low, mid, high = ordered[0], median(ordered), ordered[-1]
    if len(ordered) == 1:
        return f"{mid:,.{digits}f}"
    return f"{mid:,.{digits}f} [{low:,.{digits}f}{DASH}{high:,.{digits}f}]"


def _metric(trials: list[dict[str, Any]], read: Callable[[dict[str, Any]], float]) -> float:
    return float(median(read(trial) for trial in trials))


def _machine(trial: dict[str, Any]) -> str:
    env = trial["environment"]
    kind = "virtualized" if env["virtualized"] else "bare-metal"
    return (
        f"one {kind} host: {env['cpu_model']}, {env['logical_cpus']} logical CPUs, "
        f"{env['memory_total_gib']} GiB RAM, {env['os']}, kernel {env['kernel']}, "
        f"Python {env['python']}"
    )


def _build(trials: Iterable[dict[str, Any]]) -> str:
    trials = list(trials)
    shas = sorted({trial["environment"]["git_sha"][:7] for trial in trials})
    dirty = any(trial["environment"]["git_dirty"] for trial in trials)
    return f"commit {', '.join(shas)}, {'dirty' if dirty else 'clean'} checkout"


# scaling: m21


def _speedups(pools: dict[int, list[dict[str, Any]]], workers: int) -> list[float]:
    base = pools[min(pools)]
    return [
        trial["metrics"]["tasks_per_minute"] / first["metrics"]["tasks_per_minute"]
        for first, trial in zip(base, pools[workers], strict=True)
    ]


def _scaling_rows(pools: dict[int, list[dict[str, Any]]]) -> list[list[str]]:
    rows = []
    for workers, trials in pools.items():
        speedup = _speedups(pools, workers)
        rows.append(
            [
                str(workers),
                _spread(t["metrics"]["tasks_per_minute"] for t in trials),
                "1" if workers == min(pools) else _spread(speedup, 2),
                f"{_metric(trials, lambda t: t['metrics']['queue_wait_seconds']['p50']):.2f} / "
                f"{_metric(trials, lambda t: t['metrics']['queue_wait_seconds']['p95']):.2f} s",
                f"{_metric(trials, lambda t: t['metrics']['utilization']):.3f}",
                f"{_metric(trials, lambda t: t['resources']['host_busy_fraction']):.1%}",
                f"{_metric(trials, lambda t: t['resources']['cpu_wait_fraction']):.1%}",
            ]
        )
    return rows


SCALING_COLUMNS = [
    "workers",
    "tasks / min",
    "speedup",
    "queue wait p50 / p95",
    "utilization",
    "host CPU busy",
    "a task waiting for a CPU",
]


def _after_last(trial: dict[str, Any]) -> float:
    """The share of the workers' time idle after each one's last job, until the batch ended."""
    wall = trial["metrics"]["batch_wall_clock_seconds"]
    last: dict[str, float] = {}
    for job in trial["jobs"]:
        last[job["worker"]] = max(last.get(job["worker"], 0.0), job["finished_at"])
    workers = trial["config"]["workers"]
    # a worker that never got a job sat idle the whole batch
    idle = sum(wall - finished for finished in last.values()) + (workers - len(last)) * wall
    return float(idle / (workers * wall))


def _cpu_per_job(trials: list[dict[str, Any]]) -> float:
    return float(median(t["resources"]["host_busy_cpu_seconds"] / len(t["jobs"]) for t in trials))


def _scaling() -> tuple[dict[str, Any], dict[str, Any]]:
    root = RESULTS / "bench" / "m21"
    zero, past = _pools(root / "zero"), _pools(root / "past-cores")
    recorded, containers = _pools(root / "recorded"), _pools(root / "containers")
    every = [
        trial for run in (zero, past, recorded, containers) for t in run.values() for trial in t
    ]
    first = zero[min(zero)][0]
    jobs = first["config"]["tasks"]

    def table(title: str, pools: dict[int, list[dict[str, Any]]]) -> Table:
        trials = sorted({len(t) for t in pools.values()})
        return {
            "title": f"{title}, {jobs} jobs a batch, {trials[0]} trials per pool",
            "columns": SCALING_COLUMNS,
            "rows": _scaling_rows(pools),
        }

    rates = {w: _metric(t, lambda t: t["metrics"]["tasks_per_minute"]) for w, t in zero.items()}
    p50 = {
        w: _metric(t, lambda t: t["metrics"]["queue_wait_seconds"]["p50"]) for w, t in zero.items()
    }
    four = zero[4]
    busy = _metric(four, lambda t: t["resources"]["host_busy_fraction"])
    own = median(
        t["resources"]["processes"]["workers"] / t["resources"]["host_busy_cpu_seconds"]
        for t in four
    )
    eight = median(_speedups(past, 8))
    eight_wait = _metric(past[8], lambda t: t["resources"]["cpu_wait_fraction"])
    four_wait = _metric(past[4], lambda t: t["resources"]["cpu_wait_fraction"])
    tail = median(_after_last(t) for t in recorded[16])
    sixteen_busy = _metric(recorded[16], lambda t: t["resources"]["host_busy_fraction"])
    boxed_busy = _metric(containers[16], lambda t: t["resources"]["host_busy_fraction"])
    config = (
        f"{_machine(first)}; every process — the fleet workers, its api, PostgreSQL 16 and the "
        f"bench — on that one host, not a cluster; {jobs} jobs a batch (the 18-task set four "
        f"times, seed {first['config']['seed']}), every pool's trials interleaved with the order "
        f"reversed every other trial; {_build(every)}"
    )
    headline = {
        "id": "scaling",
        "claim": "Throughput and queue wait at 1, 2 and 4 workers, and what limits them",
        "value": (
            f"{rates[1]:.1f} → {rates[2]:.1f} → {rates[4]:.1f} tasks/min at 1, 2, 4 workers "
            f"({median(_speedups(zero, 4)):.2f}{TIMES}); median queue wait "
            f"{p50[1]:.1f} → {p50[2]:.1f} → {p50[4]:.1f} s"
        ),
        "detail": (
            f"The bottleneck, measured: the host's CPUs. At 4 workers they were {busy:.1%} busy, "
            f"{own:.0%} of it the workers' own agent loops and sandboxed commands; 8 workers did "
            f"{eight:.2f}{TIMES} the work of 4, with a runnable task waiting for a CPU "
            f"{eight_wait:.1%} of the time against {four_wait:.1%}."
        ),
        "config": config,
        "sources": [
            _relative(root / "zero"),
            _relative(root / "past-cores"),
            _relative(root / "analysis.py"),
        ],
        "doc": "docs/m21-scaling.md",
    }
    section = {
        "id": "scaling",
        "title": "Scaling on one host",
        "config": config,
        "points": [
            headline["detail"],
            (
                f"At the model's recorded latency the CPUs were {sixteen_busy:.1%} busy at 16 "
                f"workers; the loss there was the batch's own tail, {tail:.1%} of the workers' "
                f"time idle after their last job while the longest jobs finished."
            ),
            (
                f"In containers a job cost {_cpu_per_job(containers[4]):.2f} CPU-seconds at 4 "
                f"workers against {_cpu_per_job(recorded[4]):.2f} in a worker's own process, and "
                f"16 workers kept the CPUs {boxed_busy:.1%} busy."
            ),
        ],
        "tables": [
            table("Jobs at full speed (zero model latency)", zero),
            table("4 against 8 workers at full speed, past the core count", past),
            table("Jobs at the model's recorded latency", recorded),
            table("Each job in a container of its own, at recorded latency", containers),
        ],
        "sources": [_relative(root), _relative(root / "analysis.py")],
        "doc": "docs/m21-scaling.md",
    }
    return headline, section


# fault injection: m20


def _faults() -> tuple[dict[str, Any], dict[str, Any]]:
    root = RESULTS / "chaos" / "m20"
    records = [_load(path) for path in sorted(root.glob("*/seed-*.json"))]
    order = list(dict.fromkeys(record["scenario"] for record in records))
    rows = []
    for name in order:
        runs = [record for record in records if record["scenario"] == name]
        violations = sum(len(found) for r in runs for found in r["violations"].values())
        rows.append(
            [
                name,
                runs[0]["point"],
                runs[0]["action"],
                str(len(runs)),
                str(sum(r["faults_injected"] for r in runs)),
                str(violations),
            ]
        )
    injected = sum(record["faults_injected"] for record in records)
    planned = sum(record["config"]["faults_planned"] for record in records)
    violations = sum(len(found) for r in records for found in r["violations"].values())
    kinds = Counter(
        fault["action"]
        for record in records
        for fault in record["faults"]
        if fault["point"] == record["point"]
    )
    by_kind = {
        "kills": kinds.get("kill", 0),
        "dropped connections": kinds.get("drop", 0),
        "pauses and hangs": kinds.get("stop", 0) + kinds.get("hang", 0),
        "Postgres restarts": kinds.get("restart", 0),
    }
    seeds = sorted({record["seed"] for record in records})
    shas = sorted({record["build"]["git_sha"][:7] for record in records})
    dirty = any(record["build"]["git_dirty"] for record in records)
    config = (
        f"{len(records)} runs: {len(order)} scenarios {TIMES} {len(seeds)} seeds; each run "
        f"{records[0]['config']['jobs']} jobs on {records[0]['config']['workers']} worker "
        f"processes with {records[0]['config']['lease_seconds']:g} s leases, the api and a local "
        f"PostgreSQL "
        f"on one host; commit {', '.join(shas)}, {'dirty' if dirty else 'clean'} checkout; every "
        f"scenario also runs in CI on every push"
    )
    kinds_text = ", ".join(f"{count} {kind}" for kind, count in by_kind.items())
    headline = {
        "id": "faults",
        "claim": "Injected faults, and what the automated invariant checker found",
        "value": f"{injected} faults over {len(records)} runs, {violations} invariant violations",
        "detail": (
            f"{kinds_text}. After every run the checker reads the database back: exactly one "
            f"result per job, nothing lost, no stale write, accounting that adds up; {injected} of "
            f"{planned} planned faults happened, and every one left the effects it must."
        ),
        "config": config,
        "sources": [_relative(root), _relative(RESULTS / "chaos" / "analysis.py")],
        "doc": "docs/m20-fault-injection.md",
    }
    section = {
        "id": "faults",
        "title": "Fault injection",
        "config": config,
        "points": [
            headline["detail"],
            "There is no scheduler process to restart: workers pull, and Postgres holds all "
            "state, so the api is killed mid-request and Postgres is restarted instead.",
        ],
        "tables": [
            {
                "title": "The fault matrix",
                "columns": ["scenario", "point", "fault", "runs", "faults", "violations"],
                "rows": rows,
            }
        ],
        "sources": [_relative(root), _relative(RESULTS / "chaos" / "analysis.py")],
        "doc": "docs/m20-fault-injection.md",
    }
    return headline, section


# evaluation records: m22


def _experiment() -> tuple[dict[str, Any], dict[str, Any]]:
    root = RESULTS / "bench" / "m22"
    comparison = _load(root / "comparison.json")
    baseline = _trials(root / "baseline")[0]
    rows = []
    for name, key, unit, digits in (
        ("expectations met", "matched", "%", 1),
        ("tokens per job", "tokens_per_job", "", 0),
        ("cost per job at list price", "cost_per_job", "$", 5),
        ("model calls per job", "llm_calls_per_job", "", 2),
        ("service time per job", "service_seconds", "s", 2),
    ):
        interval = comparison[key]

        def show(value: float, signed: bool = False, unit: str = unit, digits: int = digits) -> str:
            scaled = value * (100 if unit == "%" else 1)
            sign = ("+" if scaled >= 0 else "-") if signed else ("-" if scaled < 0 else "")
            number = format(abs(scaled), f",.{digits}f")
            return f"{sign}${number}" if unit == "$" else f"{sign}{number}{unit}"

        relative = interval["change"] / interval["baseline"] if interval["baseline"] else 0.0
        # a share's change is in points, which a relative change would only blur
        change = (
            f"{interval['change'] * 100:+.1f} points"
            if unit == "%"
            else f"{show(interval['change'], True)} ({relative:+.1%})"
        )
        bounds = (
            f"{interval['low'] * 100:+.1f} to {interval['high'] * 100:+.1f} points"
            if unit == "%"
            else f"{show(interval['low'], True)} to {show(interval['high'], True)}"
        )
        if interval["low"] > 0:
            verdict = "better" if key == "matched" else "worse"
        elif interval["high"] < 0:
            verdict = "worse" if key == "matched" else "better"
        else:
            verdict = "no detectable change"
        rows.append(
            [
                name,
                show(interval["baseline"]),
                show(interval["candidate"]),
                change,
                bounds,
                verdict,
            ]
        )
    replays = []
    for arm in ("baseline", "short-thoughts"):
        [original] = [t for t in _trials(root / arm) if t["trial"] == 1]
        [again] = _trials(root / "replayed" / arm)
        by_id = {job["job_id"]: job for job in again["jobs"]}
        same = sum(
            (job["outcome"], job["prompt_tokens"], job["completion_tokens"], job["llm_calls"])
            == tuple(
                by_id[job["job_id"]][k]
                for k in ("outcome", "prompt_tokens", "completion_tokens", "llm_calls")
            )
            for job in original["jobs"]
        )
        replays.append(f"{same} of {len(original['jobs'])}")
    tokens = comparison["tokens_per_job"]
    config = (
        f"{baseline['config']['model']} on {baseline['config']['provider']}, the 18-task set "
        f"once a trial, seed {baseline['config']['seed']}, the two arms interleaved; one round "
        f"completed "
        f"before the provider's daily token cap; {_machine(baseline)}; {_build([baseline])}"
    )
    headline = {
        "id": "experiments",
        "claim": "Did a prompt change help, and by how much: the comparison the platform generates",
        "value": (
            f"tokens per job {tokens['baseline']:,.0f} → {tokens['candidate']:,.0f} "
            f"({tokens['change'] / tokens['baseline']:+.1%}; 95% interval {tokens['low']:+,.0f} to "
            f"{tokens['high']:+,.0f}), every expectation still met"
        ),
        "detail": (
            "One sentence added to the agent's system prompt, run against the real model with and "
            "without it. Replayed from its own recorded responses, each arm's trial came out the "
            f"same job for job: {replays[0]} without the sentence, {replays[1]} with it."
        ),
        "config": config,
        "sources": [_relative(root), _relative(root / "analysis.py")],
        "doc": "docs/m22-records.md",
    }
    section = {
        "id": "experiments",
        "title": "Reproducible evaluation records",
        "config": config,
        "points": [headline["detail"], *comparison["verdict"]],
        "tables": [
            {
                "title": f"Over {comparison['pairs']} paired jobs, round {comparison['rounds']}",
                "columns": ["measure", "without", "with", "change", "95% interval", "verdict"],
                "rows": rows,
            }
        ],
        "sources": [_relative(root), _relative(root / "analysis.py")],
        "doc": "docs/m22-records.md",
    }
    return headline, section


# the single-process baseline: m16


def _baseline() -> dict[str, Any]:
    root = RESULTS / "bench"
    rows = []
    every = []
    for label, name in (
        ("sequential-replay-zero", "replay, zero latency"),
        ("sequential-replay-recorded", "replay, recorded latency"),
        ("sequential-real-qwen3.8-27b", "real model, one trial"),
    ):
        trials = [t for t in _trials(root / label) if t["interrupted"] is None]
        every += trials
        m = [t["metrics"] for t in trials]
        rows.append(
            [
                name,
                str(len(trials)),
                _spread([x["batch_wall_clock_seconds"] for x in m], 2) + " s",
                _spread([x["tasks_per_minute"] for x in m], 2),
                f"{median(x['queue_wait_seconds']['p50'] for x in m):.2f} / "
                f"{median(x['queue_wait_seconds']['p95'] for x in m):.2f} s",
                f"{median(x['counts']['matched_expectation'] for x in m):.0f} of "
                f"{median(x['counts']['jobs'] for x in m):.0f}",
            ]
        )
    [real] = _trials(root / "sequential-real-qwen3.8-27b")
    waited = real["metrics"]["retry_wait_seconds"] / real["metrics"]["batch_wall_clock_seconds"]
    config = (
        f"one in-process worker running the 18-task set back to back, seed "
        f"{real['config']['seed']}; {_machine(real)}; {_build(every)}"
    )
    return {
        "id": "baseline",
        "title": "The single-process baseline",
        "config": config,
        "points": [
            f"With the real model ({real['config']['model']}), {waited:.0%} of the batch went to "
            f"waiting on the provider's rate limit."
        ],
        "tables": [
            {
                "title": "The 18-task set, one worker",
                "columns": [
                    "run",
                    "trials",
                    "batch",
                    "tasks / min",
                    "queue wait p50 / p95",
                    "expectations met",
                ],
                "rows": rows,
            }
        ],
        "sources": [
            _relative(root / "sequential-replay-zero"),
            _relative(root / "sequential-real-qwen3.8-27b"),
        ],
        "doc": "docs/m16-bench-harness.md",
    }


# controlled execution: m19


def _containers() -> dict[str, Any]:
    path = RESULTS / "bench" / "m19" / "container-overhead.json"
    record = _load(path)
    process, container = record["summary"]["process"], record["summary"]["container"]
    rows = [
        [name, f"{process[key]:.4f} s", f"{container[key]:.4f} s"]
        for name, key in (
            ("a job that does nothing, its whole service", "nothing_service_seconds"),
            ("starting Python", "python_startup_seconds"),
            ("importing the agent's code", "import_seconds"),
            ("a sandboxed `true`", "sandbox_true_seconds"),
            ("a sandboxed `pytest --version`", "sandbox_pytest_version_seconds"),
        )
    ]
    rounds = record["config"]["rounds"] * record["config"]["repeat_per_round"]
    env = record["environment"]
    return {
        "id": "containers",
        "title": "What a container per job costs",
        "config": (
            f"medians of {rounds} runs of each, process and container alternating; one "
            f"{'virtualized ' if env['virtualized'] else ''}host: {env['cpu_model']}, "
            f"{env['logical_cpus']} logical CPUs; commit {env['git_sha'][:7]}, "
            f"{'dirty' if env['git_dirty'] else 'clean'} checkout"
        ),
        "points": [
            "Each attempt runs in a locked-down container of its own: CPU, memory, process, "
            "scratch and time limits, and no network unless a destination is granted."
        ],
        "tables": [
            {
                "title": "In the worker's process against in a container",
                "columns": ["measured", "in process", "in a container"],
                "rows": rows,
            }
        ],
        "sources": [_relative(path)],
        "doc": "docs/m19-controlled-execution.md",
    }


def build_report() -> dict[str, Any]:
    scaling_headline, scaling = _scaling()
    faults_headline, faults = _faults()
    experiment_headline, experiment = _experiment()
    return {
        "schema_version": 1,
        "about": (
            "Every number here is computed from the records committed under docs/results by "
            "backend/bench/report.py, and a test fails if this file and the records disagree."
        ),
        "repository": REPOSITORY,
        "headline": [scaling_headline, faults_headline, experiment_headline],
        "sections": [scaling, faults, experiment, _containers(), _baseline()],
    }


def render(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPORT_PATH)
    args = parser.parse_args(argv)
    args.out.write_text(render(build_report()), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
