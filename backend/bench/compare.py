"""Did a change make the bench better or worse, and by how much: two runs compared job by job.

The runs are paired by round and job. Trial k of each ran in the same round of an interleaved
session, and the same seed planned the same jobs, so job j in round k of the baseline is set against
job j in round k of the candidate. Intervals come from resampling jobs, each with all of its rounds,
so a job that behaves the same way every round counts as one observation, not as one per round.
Each verdict follows its interval: better or worse when the interval excludes no change, and no
detectable change when it doesn't. McNemar's test is reported beside it for the flips, but it
counts each paired run on its own, and runs of one job across rounds are not independent.
"""

import difflib
import math
import random
import statistics
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any, Literal

from pydantic import BaseModel

from bench.jobs import JobResult, Outcome
from bench.metrics import percentile
from bench.records import TrialRecord

COMPARISON_SCHEMA_VERSION = 1
RESAMPLES = 10_000

Pair = tuple[JobResult, JobResult]


class Interval(BaseModel):
    """A per-job mean in each run, and the paired change, candidate minus baseline, with its 95%
    interval."""

    baseline: float
    candidate: float
    change: float
    low: float
    high: float


class TaskChange(BaseModel):
    """A task whose outcomes moved: what each run made of it, over the paired runs."""

    task_id: str
    expected: str
    runs: int
    baseline: dict[str, int]
    candidate: dict[str, int]
    baseline_matched: int
    candidate_matched: int


class Comparison(BaseModel):
    schema_version: int = COMPARISON_SCHEMA_VERSION
    record: Literal["comparison"] = "comparison"
    baseline: str
    candidate: str
    # the rounds both runs completed, the jobs paired in them, and the trials left without a pair
    rounds: list[int]
    jobs: int
    pairs: int
    unpaired: dict[str, list[int]]
    # what differs between the runs' configs, as [baseline, candidate], and which of it wasn't
    # declared as the change; and the system prompts' diff
    config_changes: dict[str, list[Any]]
    unexpected_changes: list[str]
    prompt_diff: str
    # the code and machines the trials ran on, where the two runs differ
    environment_changes: dict[str, list[Any]]
    # the share of runs that met their task's expectation
    matched: Interval
    # paired runs that met it only with the change, and only without it
    better_pairs: int
    worse_pairs: int
    mcnemar_p: float
    tasks: list[TaskChange]
    tokens_per_job: Interval
    cost_per_job: Interval | None
    llm_calls_per_job: Interval
    service_seconds: Interval
    model_seconds: Interval | None
    verdict: list[str]


def mcnemar_p(worse: int, better: int) -> float:
    """The exact two-sided McNemar p-value: how likely this many one-way flips are by chance."""
    flips = worse + better
    if flips == 0:
        return 1.0
    tail = sum(math.comb(flips, i) for i in range(min(worse, better) + 1)) * 0.5**flips
    return min(1.0, 2 * tail)


def _complete(trials: Sequence[TrialRecord]) -> dict[int, TrialRecord]:
    return {trial.trial: trial for trial in trials if trial.interrupted is None}


def pair_jobs(
    baseline: Sequence[TrialRecord], candidate: Sequence[TrialRecord]
) -> tuple[list[int], dict[str, list[Pair]]]:
    """The rounds both runs completed, and each job's baseline and candidate run in each."""
    base, cand = _complete(baseline), _complete(candidate)
    rounds = sorted(base.keys() & cand.keys())
    pairs: dict[str, list[Pair]] = {}
    for round_ in rounds:
        theirs = {job.job_id: job for job in cand[round_].jobs}
        for job in base[round_].jobs:
            if job.job_id in theirs:
                pairs.setdefault(job.job_id, []).append((job, theirs[job.job_id]))
    return rounds, pairs


def interval(
    pairs: dict[str, list[Pair]],
    value: Callable[[JobResult], float],
    *,
    resamples: int = RESAMPLES,
    seed: int = 0,
) -> Interval:
    """The per-job mean in each run and their difference, with a 95% interval from resampling
    jobs, each with all of its rounds."""
    ids = sorted(pairs)
    every = [pair for job in ids for pair in pairs[job]]
    # per job, the summed difference over its rounds and how many rounds it has
    sums = {job: sum(value(c) - value(b) for b, c in pairs[job]) for job in ids}
    counts = {job: len(pairs[job]) for job in ids}
    rng = random.Random(seed)
    changes = []
    for _ in range(resamples):
        picked = [rng.choice(ids) for _ in ids]
        changes.append(sum(sums[job] for job in picked) / sum(counts[job] for job in picked))
    base = statistics.fmean(value(b) for b, _ in every)
    cand = statistics.fmean(value(c) for _, c in every)
    return Interval(
        baseline=base,
        candidate=cand,
        change=cand - base,
        low=percentile(changes, 2.5),
        high=percentile(changes, 97.5),
    )


def _outcome(job: JobResult) -> str:
    return job.outcome.value if job.outcome != Outcome.FAILED else f"failed: {job.failure_mode}"


def _moved_tasks(pairs: dict[str, list[Pair]]) -> list[TaskChange]:
    by_task: dict[str, list[Pair]] = {}
    for job_pairs in pairs.values():
        for pair in job_pairs:
            by_task.setdefault(pair[0].task_id, []).append(pair)
    moved = []
    for task_id, task_pairs in sorted(by_task.items()):
        base = Counter(_outcome(b) for b, _ in task_pairs)
        cand = Counter(_outcome(c) for _, c in task_pairs)
        base_met = sum(b.matched_expectation for b, _ in task_pairs)
        cand_met = sum(c.matched_expectation for _, c in task_pairs)
        if base != cand or base_met != cand_met:
            moved.append(
                TaskChange(
                    task_id=task_id,
                    expected=task_pairs[0][0].expected,
                    runs=len(task_pairs),
                    baseline=dict(sorted(base.items())),
                    candidate=dict(sorted(cand.items())),
                    baseline_matched=base_met,
                    candidate_matched=cand_met,
                )
            )
    return moved


def _one_config(trials: Sequence[TrialRecord], name: str) -> dict[str, Any]:
    configs = [trial.config.model_dump(mode="json") for trial in trials]
    if any(config != configs[0] for config in configs):
        raise ValueError(f"the {name} run's trials ran with different configurations")
    return configs[0]


def _prompt_diff(base: dict[str, Any], cand: dict[str, Any]) -> str:
    before, after = base.get("system_prompts") or {}, cand.get("system_prompts") or {}
    chunks = []
    for kind in sorted(before.keys() | after.keys()):
        lines = difflib.unified_diff(
            (before.get(kind) or "").splitlines(),
            (after.get(kind) or "").splitlines(),
            f"baseline/{kind}",
            f"candidate/{kind}",
            lineterm="",
        )
        chunk = "\n".join(lines)
        if chunk:
            chunks.append(chunk + "\n")
    return "".join(chunks)


def _environment_changes(
    baseline: Sequence[TrialRecord], candidate: Sequence[TrialRecord]
) -> dict[str, list[Any]]:
    changes = {}
    for field in type(baseline[0].environment).model_fields:
        base = sorted({str(getattr(trial.environment, field)) for trial in baseline})
        cand = sorted({str(getattr(trial.environment, field)) for trial in candidate})
        if base != cand:
            changes[field] = [base, cand]
    return changes


def _judge(change: Interval, *, higher_is_better: bool) -> str:
    if change.low > 0:
        return "better" if higher_is_better else "worse"
    if change.high < 0:
        return "worse" if higher_is_better else "better"
    return "no detectable change"


def _verdict(comparison: Comparison) -> list[str]:
    matched = comparison.matched
    lines = [
        f"Expectations met: {matched.baseline:.1%} of runs without the change, "
        f"{matched.candidate:.1%} with it, a change of {matched.change * 100:+.1f} points "
        f"(95% interval {matched.low * 100:+.1f} to {matched.high * 100:+.1f}) over "
        f"{comparison.pairs} paired runs of {comparison.jobs} jobs. {comparison.better_pairs} "
        f"met it only with the change and {comparison.worse_pairs} only without "
        f"(exact McNemar p = {comparison.mcnemar_p:.3f}, counting each paired run on its own): "
        f"{_judge(matched, higher_is_better=True)}."
    ]

    def line(name: str, change: Interval, unit: str, digits: int) -> str:
        relative = f", {change.change / change.baseline:+.1%}" if change.baseline else ""
        return (
            f"{name}: {change.baseline:,.{digits}f} → {change.candidate:,.{digits}f} {unit} "
            f"({change.change:+,.{digits}f}{relative}; 95% interval {change.low:+,.{digits}f} to "
            f"{change.high:+,.{digits}f}): {_judge(change, higher_is_better=False)}."
        )

    lines.append(line("Tokens per job", comparison.tokens_per_job, "tokens", 0))
    if comparison.cost_per_job is not None:
        lines.append(line("Cost per job at the pinned price", comparison.cost_per_job, "USD", 5))
    lines.append(line("Model calls per job", comparison.llm_calls_per_job, "calls", 2))
    if comparison.model_seconds is not None:
        lines.append(line("Time waiting on the model per job", comparison.model_seconds, "s", 2))
    lines.append(line("Service time per job", comparison.service_seconds, "s", 2))
    return lines


def compare(
    baseline: Sequence[TrialRecord],
    candidate: Sequence[TrialRecord],
    *,
    expect: Sequence[str] = (),
    resamples: int = RESAMPLES,
    seed: int = 0,
) -> Comparison:
    """Set a candidate run against a baseline, job by job, over the rounds both completed."""
    rounds, pairs = pair_jobs(baseline, candidate)
    if not pairs:
        raise ValueError("the two runs have no complete round with a job in common")
    base_trials = [trial for trial in baseline if trial.trial in rounds]
    cand_trials = [trial for trial in candidate if trial.trial in rounds]
    base_config = _one_config(base_trials, "baseline")
    cand_config = _one_config(cand_trials, "candidate")
    changes = {
        field: [base_config.get(field), cand_config.get(field)]
        for field in sorted(base_config.keys() | cand_config.keys())
        if base_config.get(field) != cand_config.get(field)
    }
    every = [pair for job_pairs in pairs.values() for pair in job_pairs]
    better = sum(c.matched_expectation and not b.matched_expectation for b, c in every)
    worse = sum(b.matched_expectation and not c.matched_expectation for b, c in every)

    def measure(value: Callable[[JobResult], float]) -> Interval:
        return interval(pairs, value, resamples=resamples, seed=seed)

    priced = all(b.cost_usd is not None and c.cost_usd is not None for b, c in every)
    timed = all(b.output is not None and c.output is not None for b, c in every)
    comparison = Comparison(
        baseline=baseline[0].label,
        candidate=candidate[0].label,
        rounds=rounds,
        jobs=len(pairs),
        pairs=len(every),
        unpaired={
            "baseline": sorted(t.trial for t in baseline if t.trial not in rounds),
            "candidate": sorted(t.trial for t in candidate if t.trial not in rounds),
        },
        config_changes=changes,
        unexpected_changes=sorted(set(changes) - set(expect)),
        prompt_diff=_prompt_diff(base_config, cand_config),
        environment_changes=_environment_changes(base_trials, cand_trials),
        matched=measure(lambda job: float(job.matched_expectation)),
        better_pairs=better,
        worse_pairs=worse,
        mcnemar_p=mcnemar_p(worse, better),
        tasks=_moved_tasks(pairs),
        tokens_per_job=measure(lambda job: float(job.total_tokens)),
        cost_per_job=measure(lambda job: job.cost_usd or 0.0) if priced else None,
        llm_calls_per_job=measure(lambda job: float(job.llm_calls)),
        service_seconds=measure(lambda job: job.service_time),
        model_seconds=(
            measure(lambda job: job.output.model_seconds if job.output else 0.0) if timed else None
        ),
        verdict=[],
    )
    comparison.verdict = _verdict(comparison)
    return comparison


def changed_leaves(before: Any, after: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    """Where two config values differ, down to the innermost keys that do."""
    if isinstance(before, dict) and isinstance(after, dict):
        leaves = []
        for key in sorted(before.keys() | after.keys()):
            inner = f"{path}.{key}" if path else str(key)
            leaves += changed_leaves(before.get(key), after.get(key), inner)
        return leaves
    return [] if before == after else [(path, before, after)]


def render(comparison: Comparison) -> str:
    """The comparison as markdown, for a terminal or a document."""
    lines = [
        f"# {comparison.candidate} against {comparison.baseline}",
        "",
        f"Rounds paired: {comparison.rounds}; {comparison.pairs} paired runs of "
        f"{comparison.jobs} jobs. Trials without a pair: {comparison.unpaired}.",
        "",
        "## Verdict",
        "",
        *[f"- {line}" for line in comparison.verdict],
        "",
        "## What changed",
        "",
    ]
    for field, (before, after) in comparison.config_changes.items():
        # the prompts follow as a diff
        if field != "system_prompts":
            for where, old, new in changed_leaves(before, after, field):
                lines.append(f"- `{where}`: `{old}` → `{new}`")
    if comparison.unexpected_changes:
        lines.append(f"- **not declared as the change**: {comparison.unexpected_changes}")
    if comparison.prompt_diff:
        lines += ["", "```diff", comparison.prompt_diff.rstrip("\n"), "```"]
    if comparison.environment_changes:
        lines += ["", f"Environment differences: {comparison.environment_changes}"]
    lines += ["", "## Tasks whose outcomes moved", ""]
    if comparison.tasks:
        lines += ["| Task | Expected | Runs | Baseline | Candidate |", "|---|---|---|---|---|"]
        for task in comparison.tasks:
            lines.append(
                f"| {task.task_id} | {task.expected} | {task.runs} | "
                f"{task.baseline} ({task.baseline_matched} met) | "
                f"{task.candidate} ({task.candidate_matched} met) |"
            )
    else:
        lines.append("None: every task came out the same way in every paired round.")
    return "\n".join(lines) + "\n"
