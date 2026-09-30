"""Command line for the bench: record real trials, replay recordings, summarize trials."""

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.llm.base import LLMProvider
from app.llm.groq_provider import GroqProvider
from app.sandbox.base import SandboxConfig
from bench.environment import capture_environment
from bench.executor import (
    AGENT_CONFIGS,
    BatchResult,
    Executor,
    JobCallback,
    ProviderFactory,
    SequentialExecutor,
)
from bench.fleet_executor import FleetExecutor, pool_topology
from bench.groq_limits import is_daily_cap, retry_delay
from bench.jobs import JobResult
from bench.metrics import PERCENTILE_METHOD, compute_metrics
from bench.records import (
    RESULTS_ROOT,
    BenchConfig,
    SummaryRecord,
    TrialRecord,
    load_trials,
    outcome_vector,
    summarize,
    summary_path,
    trial_path,
    utc_now,
    write_record,
)
from bench.replay import (
    RECORDINGS_ROOT,
    LatencyProfile,
    Recording,
    RecordingProvider,
    ReplayProvider,
    build_recording,
    load_recordings,
    recordings_digest,
    write_recording,
)
from bench.runner import replay_payload
from bench.taskset import BenchTask, Job, TaskSet, load_taskset, plan_jobs
from fleet.config import FleetSettings, async_url
from fleet.docker import Docker
from fleet.localdb import LocalPostgres
from fleet.migrate import migrate
from fleet.store import server_version

DEFAULT_MODEL = "qwen/qwen3.8-27b"
DEFAULT_SEED = 1
DEFAULT_TRIALS = 5

# runs one planned batch through some executor
BatchRun = Callable[[list[Job]], Awaitable[BatchResult]]


class Described(Protocol):
    name: str
    topology: str
    workers: int
    execution: dict[str, Any] | None


def build_config(
    taskset: TaskSet,
    executor: Described,
    *,
    mode: Literal["real", "replay"],
    latency: str | None,
    tasks: int,
    seed: int,
    digest_of_recordings: str | None,
    provider: str,
    model: str,
) -> BenchConfig:
    return BenchConfig(
        mode=mode,
        latency=latency,
        executor=executor.name,
        workers=executor.workers,
        topology=executor.topology,
        tasks=tasks,
        seed=seed,
        taskset_version=taskset.version,
        taskset_digest=taskset.digest(),
        recordings_digest=digest_of_recordings,
        provider=provider,
        model=model,
        agent_configs={kind.value: asdict(config) for kind, config in AGENT_CONFIGS.items()},
        sandbox_config=asdict(SandboxConfig()),
        tool_transport=get_settings().tool_transport,
        percentile_method=PERCENTILE_METHOD,
        execution=executor.execution,
    )


def _progress(total: int) -> JobCallback:
    done = 0

    def report(_task: BenchTask, _provider: LLMProvider, result: JobResult) -> None:
        nonlocal done
        done += 1
        detail = result.failure_mode or result.outcome.value
        print(
            f"  [{done}/{total}] {result.job_id:<24} {detail:<22} {result.service_time:7.1f}s "
            f"{result.llm_calls:>3} calls {result.total_tokens:>7} tokens",
            flush=True,
        )

    return report


def _both(first: JobCallback, second: JobCallback | None) -> JobCallback:
    def call(task: BenchTask, provider: LLMProvider, result: JobResult) -> None:
        first(task, provider, result)
        if second is not None:
            second(task, provider, result)

    return call


async def run_trial(
    *,
    label: str,
    trial: int,
    taskset: TaskSet,
    count: int,
    seed: int,
    execute: BatchRun,
    config: BenchConfig,
) -> TrialRecord:
    # captured before the run so the git state is the code that actually ran
    environment = capture_environment()
    started_at = utc_now()
    batch = await execute(plan_jobs(taskset, count, seed))
    return TrialRecord(
        label=label,
        trial=trial,
        started_at=started_at,
        interrupted=batch.interrupted,
        config=config,
        environment=environment,
        metrics=compute_metrics(batch.results, workers=config.workers),
        jobs=batch.results,
    )


def _replayable(
    taskset: TaskSet, recordings: dict[str, Recording], count: int, seed: int
) -> dict[str, Recording]:
    used = {task.id: recordings[task.id] for task in taskset.tasks if task.id in recordings}
    missing = sorted({job.task_id for job in plan_jobs(taskset, count, seed)} - used.keys())
    if missing:
        raise ValueError(f"no recording for {', '.join(missing)}; record the task set first")
    return used


def _replay_config(
    taskset: TaskSet,
    executor: Described,
    used: dict[str, Recording],
    *,
    latency: LatencyProfile,
    count: int,
    seed: int,
) -> BenchConfig:
    return build_config(
        taskset,
        executor,
        mode="replay",
        latency=latency.value,
        tasks=count,
        seed=seed,
        digest_of_recordings=recordings_digest(used),
        provider="replay",
        model=", ".join(sorted({recording.model for recording in used.values()})),
    )


def _replay_batch(
    executor: Executor | FleetExecutor,
    taskset: TaskSet,
    used: dict[str, Recording],
    latency: LatencyProfile,
    progress: JobCallback | None = None,
) -> BatchRun:
    """Replay a planned batch in process, or submit it to the fleet as self-contained payloads."""
    if isinstance(executor, FleetExecutor):
        fleet = executor
        return lambda jobs: fleet.run(
            taskset, jobs, lambda task: replay_payload(task, used[task.id], latency)
        )
    return lambda jobs: executor.run(
        taskset,
        jobs,
        lambda task: ReplayProvider(used[task.id], latency=latency),
        on_result=progress,
    )


async def replay_trials(
    *,
    label: str,
    taskset: TaskSet,
    recordings: dict[str, Recording],
    latency: LatencyProfile,
    trials: int,
    count: int,
    seed: int,
    out_dir: Path,
    executor: Executor | FleetExecutor | None = None,
    verbose: bool = True,
) -> SummaryRecord:
    """Replay the recordings for N trials, writing each trial record and their summary."""
    active = executor or SequentialExecutor()
    used = _replayable(taskset, recordings, count, seed)
    config = _replay_config(taskset, active, used, latency=latency, count=count, seed=seed)
    execute = _replay_batch(active, taskset, used, latency, _progress(count) if verbose else None)
    records = []
    for trial in range(1, trials + 1):
        if verbose:
            print(f"trial {trial}/{trials}", flush=True)
        record = await run_trial(
            label=label,
            trial=trial,
            taskset=taskset,
            count=count,
            seed=seed,
            execute=execute,
            config=config,
        )
        write_record(record, trial_path(out_dir, trial))
        records.append(record)
    summary = summarize(label, records)
    write_record(summary, summary_path(out_dir))
    return summary


def _daily_cap_reached(provider: LLMProvider) -> str | None:
    gave_up = provider.gave_up if isinstance(provider, RecordingProvider) else None
    if gave_up is not None and is_daily_cap(gave_up):
        return "the provider's daily cap was reached"
    return None


async def record_trial(
    *,
    label: str,
    trial: int,
    taskset: TaskSet,
    count: int,
    seed: int,
    inner_for: ProviderFactory,
    provider: str,
    model: str,
    out_dir: Path,
    recordings_dir: Path | None,
    verbose: bool = True,
) -> TrialRecord:
    """Run one real trial, optionally saving every task's responses as the replay recordings."""
    if recordings_dir is not None and count != len(taskset.tasks):
        raise ValueError("recordings need exactly one run of every task")
    executor = SequentialExecutor()
    config = build_config(
        taskset,
        executor,
        mode="real",
        latency=None,
        tasks=count,
        seed=seed,
        digest_of_recordings=None,
        provider=provider,
        model=model,
    )
    git_sha = capture_environment().git_sha

    def keep(task: BenchTask, used: LLMProvider, result: JobResult) -> None:
        if recordings_dir is None or not isinstance(used, RecordingProvider):
            return
        recording = build_recording(
            used,
            task_id=task.id,
            taskset_version=taskset.version,
            outcome=result.outcome.value,
            git_sha=git_sha,
            recorded_at=utc_now(),
        )
        write_recording(recording, recordings_dir)

    record = await run_trial(
        label=label,
        trial=trial,
        taskset=taskset,
        count=count,
        seed=seed,
        execute=lambda jobs: executor.run(
            taskset,
            jobs,
            # the recorder owns retries so each call's latency leaves out rate-limit waits
            lambda task: RecordingProvider(inner_for(task), retry_delay=retry_delay),
            stop_check=_daily_cap_reached,
            on_result=_both(keep, _progress(count) if verbose else None),
        ),
        config=config,
    )
    write_record(record, trial_path(out_dir, trial))
    trials = load_trials(out_dir)
    if any(existing.interrupted is None for existing in trials):
        write_record(summarize(label, trials), summary_path(out_dir))
    return record


def summarize_label(label: str, out_dir: Path) -> SummaryRecord:
    summary = summarize(label, load_trials(out_dir))
    write_record(summary, summary_path(out_dir))
    return summary


def replay_passed(summary: SummaryRecord) -> bool:
    """Replay is only trustworthy with identical outcomes and no divergence in any trial."""
    return summary.outcomes_identical and summary.metrics["counts.failed_harness"].max == 0


@dataclass(slots=True)
class AbResult:
    first: SummaryRecord
    second: SummaryRecord
    # every trial of both arms produced the same outcome for every job
    outcomes_match: bool


def arm_label(executor: Executor | FleetExecutor, latency: LatencyProfile) -> str:
    """ab-sequential-replay-zero, ab-fleet-1w-replay-zero, ab-fleet-1w-container-replay-zero"""
    name = executor.name
    if isinstance(executor, FleetExecutor):
        name = f"fleet-{executor.workers}w" + ("-container" if executor.mode == "container" else "")
    return f"ab-{name}-replay-{latency.value}"


async def ab_trials(
    *,
    taskset: TaskSet,
    recordings: dict[str, Recording],
    latency: LatencyProfile,
    trials: int,
    count: int,
    seed: int,
    out_root: Path,
    first: Executor | FleetExecutor,
    second: Executor | FleetExecutor,
    verbose: bool = True,
) -> AbResult:
    """Interleave two arms' trials in one session, alternating which arm goes first."""
    used = _replayable(taskset, recordings, count, seed)
    arms: dict[str, tuple[str, BenchConfig, BatchRun]] = {
        arm: (
            arm_label(executor, latency),
            _replay_config(taskset, executor, used, latency=latency, count=count, seed=seed),
            _replay_batch(executor, taskset, used, latency),
        )
        for arm, executor in (("first", first), ("second", second))
    }
    records: dict[str, list[TrialRecord]] = {arm: [] for arm in arms}
    for trial in range(1, trials + 1):
        # alternating the order keeps warm-up and drift from favoring one arm
        order = ("first", "second") if trial % 2 else ("second", "first")
        for arm in order:
            label, config, execute = arms[arm]
            record = await run_trial(
                label=label,
                trial=trial,
                taskset=taskset,
                count=count,
                seed=seed,
                execute=execute,
                config=config,
            )
            write_record(record, trial_path(out_root / label, trial))
            records[arm].append(record)
            if verbose:
                wall = record.metrics.batch_wall_clock_seconds
                print(f"trial {trial}/{trials} {label:<36} batch {wall:6.2f}s", flush=True)
    summaries = {}
    for arm, (label, _config, _execute) in arms.items():
        summaries[arm] = summarize(label, records[arm])
        write_record(summaries[arm], summary_path(out_root / label))
    reference = outcome_vector(records["first"][0])
    every = records["first"] + records["second"]
    return AbResult(
        first=summaries["first"],
        second=summaries["second"],
        outcomes_match=all(outcome_vector(record) == reference for record in every),
    )


async def _server_version(url: str) -> str:
    engine = create_async_engine(async_url(url))
    try:
        return await server_version(engine)
    finally:
        await engine.dispose()


async def _image_id(image: str) -> str | None:
    docker = Docker(FleetSettings().docker_socket)
    try:
        return await docker.image_id(image)
    finally:
        await docker.aclose()


@contextmanager
def _fleet(
    database_url: str | None, workers: int, modes: Sequence[Literal["process", "container"]]
) -> Iterator[list[FleetExecutor]]:
    """Fleets on the given database, or on a throwaway local Postgres when there is none."""
    cluster = None if database_url else LocalPostgres.start()
    url: str = database_url or (cluster.url if cluster else "")
    try:
        migrate(url)
        version = asyncio.run(_server_version(url))
        fleets = []
        for mode in modes:
            image = FleetSettings().task_image
            image_id = asyncio.run(_image_id(image)) if mode == "container" else None
            if mode == "container" and image_id is None:
                raise RuntimeError(f"no {image} image; build it with scripts/build-task-image.sh")
            topology = pool_topology(workers, version, containers=mode == "container")
            fleets.append(
                FleetExecutor(
                    url,
                    workers=workers,
                    topology=topology,
                    mode=mode,
                    task_image=image,
                    image_id=image_id,
                )
            )
        yield fleets
    finally:
        if cluster is not None:
            cluster.stop()


def _run_ab(args: argparse.Namespace, taskset: TaskSet, count: int) -> int:
    # sequential against a one-worker fleet, or that fleet in process against in containers
    modes: list[Literal["process", "container"]] = (
        ["process"] if args.arms == "sequential-fleet" else ["process", "container"]
    )
    with _fleet(args.database_url, 1, modes) as fleets:
        first: Executor | FleetExecutor = fleets[0] if len(fleets) == 2 else SequentialExecutor()
        result = asyncio.run(
            ab_trials(
                taskset=taskset,
                recordings=load_recordings(args.recordings or RECORDINGS_ROOT / taskset.version),
                latency=LatencyProfile(args.latency),
                trials=args.trials,
                count=count,
                seed=args.seed,
                out_root=args.out_root or RESULTS_ROOT,
                first=first,
                second=fleets[-1],
            )
        )
    print_summary(result.first)
    print_summary(result.second)
    print(f"per-job outcomes match across both arms: {result.outcomes_match}")
    both = replay_passed(result.first) and replay_passed(result.second)
    return 0 if result.outcomes_match and both else 1


def _replay(
    args: argparse.Namespace,
    taskset: TaskSet,
    count: int,
    executor: Executor | FleetExecutor,
    name: str,
) -> SummaryRecord:
    label = args.label or f"{name}-replay-{args.latency}"
    return asyncio.run(
        replay_trials(
            label=label,
            taskset=taskset,
            recordings=load_recordings(args.recordings or RECORDINGS_ROOT / taskset.version),
            latency=LatencyProfile(args.latency),
            trials=args.trials,
            count=count,
            seed=args.seed,
            out_dir=args.out or RESULTS_ROOT / label,
            executor=executor,
        )
    )


def print_summary(summary: SummaryRecord) -> None:
    def stat(name: str, digits: int = 2) -> str:
        value = summary.metrics[name]
        return f"{value.median:.{digits}f} [{value.min:.{digits}f}-{value.max:.{digits}f}]"

    config = summary.config
    workers = "1 worker" if config.workers == 1 else f"{config.workers} workers"
    print(
        f"{summary.label}: {config.mode} mode, latency={config.latency}, {workers}, "
        f"{config.tasks} jobs, seed {config.seed}, trials {summary.trials}"
    )
    print(f"  batch wall clock (s)       {stat('batch_wall_clock_seconds')}")
    print(f"  tasks per minute           {stat('tasks_per_minute')}")
    print(f"  queue wait p50 / p95 (s)   {stat('queue_wait_seconds.p50')} / ", end="")
    print(stat("queue_wait_seconds.p95"))
    print(f"  service p50 / p95 (s)      {stat('service_time_seconds.p50')} / ", end="")
    print(stat("service_time_seconds.p95"))
    print(f"  service max (s)            {stat('service_time_seconds.max')}")
    print(f"  utilization                {stat('utilization', 3)}")
    counts = ("solved", "escalated", "failed_task", "failed_infra", "failed_harness")
    print("  " + "  ".join(f"{name} {stat(f'counts.{name}', 0)}" for name in counts))
    print(f"  outcomes identical across trials: {summary.outcomes_identical}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m bench.cli", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    record = commands.add_parser("record", help="run the task set against the real model")
    record.add_argument("--trial", type=int, required=True)
    record.add_argument("--model", default=DEFAULT_MODEL)
    record.add_argument("--label", default=None)
    record.add_argument("--tasks", type=int, default=None, help="jobs (default: each task once)")
    record.add_argument("--seed", type=int, default=DEFAULT_SEED)
    record.add_argument(
        "--write-recordings", action="store_true", help="save the responses as the replay set"
    )
    record.add_argument("--out", type=Path, default=None)

    replay = commands.add_parser("replay", help="replay the recordings through the executor")
    replay.add_argument("--latency", choices=[p.value for p in LatencyProfile], default="zero")
    replay.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    replay.add_argument("--executor", choices=["sequential", "fleet"], default="sequential")
    replay.add_argument(
        "--execution",
        choices=["process", "container"],
        default="process",
        help="fleet only: run each job in the worker's process or in a container of its own",
    )
    replay.add_argument("--workers", type=int, default=1)
    replay.add_argument(
        "--database-url", default=None, help="fleet only; default: a throwaway local Postgres"
    )
    replay.add_argument("--label", default=None)
    replay.add_argument("--tasks", type=int, default=None, help="jobs (default: each task once)")
    replay.add_argument("--seed", type=int, default=DEFAULT_SEED)
    replay.add_argument("--recordings", type=Path, default=None)
    replay.add_argument("--out", type=Path, default=None)

    summary = commands.add_parser("summarize", help="rebuild a label's summary from its trials")
    summary.add_argument("--label", required=True)
    summary.add_argument("--out", type=Path, default=None)

    ab = commands.add_parser("ab", help="interleave two arms' replays in one session")
    ab.add_argument(
        "--arms",
        choices=["sequential-fleet", "process-container"],
        default="sequential-fleet",
        help="sequential against a one-worker fleet, or that fleet in process against containers",
    )
    ab.add_argument("--latency", choices=[p.value for p in LatencyProfile], default="zero")
    ab.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    ab.add_argument("--tasks", type=int, default=None, help="jobs (default: each task once)")
    ab.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ab.add_argument("--recordings", type=Path, default=None)
    ab.add_argument("--out-root", type=Path, default=None)
    ab.add_argument("--database-url", default=None, help="default: a throwaway local Postgres")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "summarize":
        print_summary(summarize_label(args.label, args.out or RESULTS_ROOT / args.label))
        return 0

    taskset = load_taskset()
    count = args.tasks or len(taskset.tasks)
    if args.command == "ab":
        return _run_ab(args, taskset, count)
    if args.command == "record":
        api_key = get_settings().groq_api_key
        if not api_key:
            parser.error("GROQ_API_KEY is not set")
        model = args.model
        label = args.label or f"sequential-real-{model.rsplit('/', 1)[-1]}"
        record = asyncio.run(
            record_trial(
                label=label,
                trial=args.trial,
                taskset=taskset,
                count=count,
                seed=args.seed,
                inner_for=lambda _task: GroqProvider(api_key, model=model, max_retries=0),
                provider="groq",
                model=model,
                out_dir=args.out or RESULTS_ROOT / label,
                recordings_dir=RECORDINGS_ROOT / taskset.version if args.write_recordings else None,
            )
        )
        if record.interrupted:
            print(f"trial {args.trial} interrupted: {record.interrupted}")
            return 1
        print(f"trial {args.trial} done: {record.metrics.counts.model_dump()}")
        return 0

    if args.executor == "fleet":
        if args.workers < 1:
            parser.error("the fleet needs at least one worker")
        with _fleet(args.database_url, args.workers, [args.execution]) as (fleet,):
            suffix = "-container" if args.execution == "container" else ""
            summary = _replay(args, taskset, count, fleet, f"fleet-{args.workers}w{suffix}")
    else:
        try:
            executor = SequentialExecutor(args.workers)
        except ValueError as exc:
            parser.error(str(exc))
        summary = _replay(args, taskset, count, executor, "sequential")
    print_summary(summary)
    return 0 if replay_passed(summary) else 1


if __name__ == "__main__":
    sys.exit(main())
