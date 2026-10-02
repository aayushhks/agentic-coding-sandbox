"""Versioned, machine-readable records of bench trials and their summaries."""

import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from bench.environment import REPO_ROOT, Environment
from bench.jobs import JobResult
from bench.metrics import TrialMetrics
from bench.prices import Price
from bench.resources import BatchResources, DatabaseCalls

RECORD_SCHEMA_VERSION = 1
RESULTS_ROOT = REPO_ROOT / "docs" / "results" / "bench"


class BenchConfig(BaseModel):
    mode: Literal["real", "replay"]
    # replay latency profile, None for real runs
    latency: str | None
    executor: str
    workers: int
    topology: str
    tasks: int
    seed: int
    taskset_version: str
    taskset_digest: str
    recordings_digest: str | None
    provider: str
    model: str
    agent_configs: dict[str, dict[str, Any]]
    sandbox_config: dict[str, Any]
    tool_transport: str
    percentile_method: str
    # where jobs ran and under what limits: the execution mode, task image and policy, if any
    execution: dict[str, Any] | None = None
    # the system prompt each kind of task started from, and a digest of those and the configs
    system_prompts: dict[str, str] | None = None
    agent_digest: str | None = None
    # the pinned price the run's cost is computed at, None when the list has none for the model
    price: Price | None = None


class TrialRecord(BaseModel):
    schema_version: int = RECORD_SCHEMA_VERSION
    record: Literal["trial"] = "trial"
    label: str
    trial: int
    started_at: str
    # why a trial stopped before running every job, e.g. the provider's daily cap
    interrupted: str | None = None
    config: BenchConfig
    environment: Environment
    metrics: TrialMetrics
    jobs: list[JobResult]
    # the cpu the batch used and the time its workers waited on the database; fleet runs only
    resources: BatchResources | None = None
    database: dict[str, DatabaseCalls] | None = None


class Stat(BaseModel):
    median: float
    min: float
    max: float


class SummaryRecord(BaseModel):
    schema_version: int = RECORD_SCHEMA_VERSION
    record: Literal["summary"] = "summary"
    label: str
    trials: list[int]
    interrupted_trials: list[int]
    config: BenchConfig
    environments: list[Environment]
    # every trial produced the same outcome for every job
    outcomes_identical: bool
    metrics: dict[str, Stat]


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def outcome_vector(record: TrialRecord) -> list[tuple[str, str, str | None]]:
    return sorted((job.job_id, job.outcome.value, job.failure_mode) for job in record.jobs)


def flatten_scalars(value: Any, prefix: str = "") -> dict[str, float]:
    """Nested metrics as dotted names, skipping series and missing values."""
    if isinstance(value, dict):
        flat: dict[str, float] = {}
        for key, inner in value.items():
            flat.update(flatten_scalars(inner, f"{prefix}.{key}" if prefix else key))
        return flat
    if isinstance(value, int | float) and not isinstance(value, bool):
        return {prefix: float(value)}
    return {}


def trial_scalars(trial: TrialRecord) -> dict[str, float]:
    """A trial's metrics, and its resources and database time when it has them, as dotted names."""
    flat = flatten_scalars(trial.metrics.model_dump(mode="json"))
    if trial.resources is not None:
        flat |= flatten_scalars(trial.resources.model_dump(mode="json"), "resources")
    if trial.database is not None:
        calls = {name: stats.model_dump() for name, stats in trial.database.items()}
        flat |= flatten_scalars(calls, "database")
    return flat


def summarize(label: str, trials: list[TrialRecord]) -> SummaryRecord:
    """Median and range of every scalar metric across the complete trials of one config."""
    complete = [trial for trial in trials if trial.interrupted is None]
    if not complete:
        raise ValueError("there are no complete trials to summarize")
    config = complete[0].config
    if any(trial.config != config for trial in complete):
        raise ValueError("trials with different configurations cannot be summarized together")
    samples = [trial_scalars(trial) for trial in complete]
    # only what every trial measured, since a process tree can go unmeasured in one of them
    names = set(samples[0]).intersection(*samples[1:])
    metrics = {
        name: Stat(
            median=statistics.median(sample[name] for sample in samples),
            min=min(sample[name] for sample in samples),
            max=max(sample[name] for sample in samples),
        )
        for name in sorted(names)
    }
    first_outcomes = outcome_vector(complete[0])
    return SummaryRecord(
        label=label,
        trials=[trial.trial for trial in complete],
        interrupted_trials=[trial.trial for trial in trials if trial.interrupted is not None],
        config=config,
        environments=[trial.environment for trial in complete],
        outcomes_identical=all(outcome_vector(trial) == first_outcomes for trial in complete),
        metrics=metrics,
    )


def trial_path(directory: Path, trial: int) -> Path:
    return directory / f"trial-{trial}.json"


def summary_path(directory: Path) -> Path:
    return directory / "summary.json"


def write_record(record: BaseModel, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def load_trials(directory: Path) -> list[TrialRecord]:
    records = [
        TrialRecord.model_validate_json(path.read_text(encoding="utf-8"))
        for path in directory.glob("trial-*.json")
    ]
    return sorted(records, key=lambda record: record.trial)
