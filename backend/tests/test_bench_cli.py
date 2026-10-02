import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import groq
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

import bench.cli
import bench.prices
from app.llm.base import CompletionResult, LLMProvider, Message
from bench.cli import (
    AbResult,
    ab_trials,
    main,
    record_trial,
    replay_passed,
    replay_trials,
    trial_order,
)
from bench.executor import SequentialExecutor
from bench.fleet_executor import FleetExecutor
from bench.jobs import FailureKind, Outcome
from bench.records import (
    BenchConfig,
    SummaryRecord,
    TrialRecord,
    load_trials,
    summarize,
    summary_path,
    trial_path,
    write_record,
)
from bench.replay import LatencyProfile, load_recordings, recordings_digest
from bench.taskset import BenchTask, plan_jobs
from tests.bench_helpers import (
    MINI_TASKSET,
    make_job,
    make_trial,
    record_mini_batch,
    scripted_provider,
)


class _DailyCappedProvider(LLMProvider):
    @property
    def name(self) -> str:
        return "groq"

    @property
    def model(self) -> str:
        return "m"

    async def complete(
        self, messages: Sequence[Message], *, temperature: float = 0.0, max_tokens: int = 1024
    ) -> CompletionResult:
        request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        response = httpx.Response(429, request=request)
        raise groq.RateLimitError(
            "Rate limit reached on tokens per day (TPD)", response=response, body=None
        )


async def test_replay_trials_write_every_trial_and_a_summary(tmp_path: Path) -> None:
    recordings = await record_mini_batch()
    summary = await replay_trials(
        label="mini",
        taskset=MINI_TASKSET,
        recordings=recordings,
        latency=LatencyProfile.ZERO,
        trials=2,
        count=3,
        seed=1,
        out_dir=tmp_path,
        verbose=False,
    )
    assert [record.trial for record in load_trials(tmp_path)] == [1, 2]
    assert summary_path(tmp_path).is_file()
    assert summary.config.mode == "replay"
    assert summary.config.recordings_digest == recordings_digest(recordings)
    assert summary.metrics["counts.jobs"].median == 3
    assert replay_passed(summary)


async def test_replay_needs_a_recording_for_every_planned_task(tmp_path: Path) -> None:
    recordings = await record_mini_batch()
    del recordings["adder"]
    with pytest.raises(ValueError, match="no recording for adder"):
        await replay_trials(
            label="mini",
            taskset=MINI_TASKSET,
            recordings=recordings,
            latency=LatencyProfile.ZERO,
            trials=1,
            count=3,
            seed=1,
            out_dir=tmp_path,
            verbose=False,
        )


async def test_a_recorded_trial_replays_to_the_same_outcomes(tmp_path: Path) -> None:
    record = await record_trial(
        label="mini-real",
        trial=1,
        taskset=MINI_TASKSET,
        count=3,
        seed=1,
        inner_for=scripted_provider,
        provider="mock",
        model="mock-model",
        out_dir=tmp_path / "records",
        recordings_dir=tmp_path / "recordings",
        verbose=False,
    )
    assert record.interrupted is None
    assert record.config.mode == "real"
    assert trial_path(tmp_path / "records", 1).is_file()
    assert summary_path(tmp_path / "records").is_file()
    recordings = load_recordings(tmp_path / "recordings")
    assert {task_id: r.outcome for task_id, r in recordings.items()} == {
        job.task_id: job.outcome.value for job in record.jobs
    }

    summary = await replay_trials(
        label="mini-replay",
        taskset=MINI_TASKSET,
        recordings=recordings,
        latency=LatencyProfile.ZERO,
        trials=2,
        count=3,
        seed=1,
        out_dir=tmp_path / "replay",
        verbose=False,
    )
    replayed = load_trials(tmp_path / "replay")[0]
    assert {job.job_id: job.outcome for job in replayed.jobs} == {
        job.job_id: job.outcome for job in record.jobs
    }
    assert replay_passed(summary)


async def test_a_trial_with_an_extra_rule_replays_with_it_and_diverges_without(
    tmp_path: Path,
) -> None:
    rule = "Keep the thought field to one short sentence."
    record = await record_trial(
        label="mini-rule",
        trial=1,
        taskset=MINI_TASKSET,
        count=3,
        seed=1,
        inner_for=scripted_provider,
        provider="mock",
        model="mock-model",
        out_dir=tmp_path / "records",
        recordings_dir=tmp_path / "recordings",
        verbose=False,
        extra_rules=[rule],
    )
    config = record.config
    assert all(list(c["extra_rules"]) == [rule] for c in config.agent_configs.values())
    assert config.system_prompts is not None
    assert all(p.endswith(f"- {rule}") for p in config.system_prompts.values())
    recordings = load_recordings(tmp_path / "recordings")

    async def replay(label: str, rules: list[str]) -> SummaryRecord:
        return await replay_trials(
            label=label,
            taskset=MINI_TASKSET,
            recordings=recordings,
            latency=LatencyProfile.ZERO,
            trials=1,
            count=3,
            seed=1,
            out_dir=tmp_path / label,
            verbose=False,
            extra_rules=rules,
        )

    same = await replay("with", [rule])
    assert replay_passed(same)
    assert same.config.agent_digest == config.agent_digest
    # without the rule the system prompt differs from the one recorded, so replay refuses it
    plain = await replay("without", [])
    assert plain.metrics["counts.failed_harness"].max == 3
    assert plain.config.agent_digest != config.agent_digest


async def test_a_trial_prices_every_job_at_the_pinned_price_and_adds_them_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prices = tmp_path / "prices.json"
    entry = {
        "provider": "mock",
        "input_per_million_tokens": 1.0,
        "output_per_million_tokens": 2.0,
        "source": "test",
        "retrieved": "today",
        "how": "made up for the test",
    }
    prices.write_text(json.dumps({"currency": "USD", "models": {"mock-model": entry}}))
    monkeypatch.setattr(bench.prices, "PRICES_PATH", prices)
    record = await record_trial(
        label="mini-priced",
        trial=1,
        taskset=MINI_TASKSET,
        count=3,
        seed=1,
        inner_for=scripted_provider,
        provider="mock",
        model="mock-model",
        out_dir=tmp_path / "records",
        recordings_dir=None,
        verbose=False,
    )
    assert record.config.price is not None and record.config.price.source == "test"
    for job in record.jobs:
        assert job.cost_usd == pytest.approx((job.prompt_tokens + 2 * job.completion_tokens) / 1e6)
    assert record.metrics.cost_usd == pytest.approx(sum(job.cost_usd or 0 for job in record.jobs))


async def test_recording_stops_at_the_daily_cap_and_keeps_finished_tasks(
    tmp_path: Path,
) -> None:
    first = plan_jobs(MINI_TASKSET, 3, seed=1)[0].task_id

    def inner_for(task: BenchTask) -> LLMProvider:
        return scripted_provider(task) if task.id == first else _DailyCappedProvider()

    record = await record_trial(
        label="mini-real",
        trial=1,
        taskset=MINI_TASKSET,
        count=3,
        seed=1,
        inner_for=inner_for,
        provider="mock",
        model="mock-model",
        out_dir=tmp_path / "records",
        recordings_dir=tmp_path / "recordings",
        verbose=False,
    )
    assert record.interrupted == "the provider's daily cap was reached after 1 of 3 jobs"
    assert [job.task_id for job in record.jobs] == [first]
    assert set(load_recordings(tmp_path / "recordings")) == {first}
    assert not summary_path(tmp_path / "records").exists()


async def test_recordings_need_each_task_exactly_once(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one run of every task"):
        await record_trial(
            label="mini-real",
            trial=1,
            taskset=MINI_TASKSET,
            count=2,
            seed=1,
            inner_for=scripted_provider,
            provider="mock",
            model="mock-model",
            out_dir=tmp_path,
            recordings_dir=tmp_path,
            verbose=False,
        )


def test_replay_passes_only_without_divergence_and_with_identical_outcomes() -> None:
    divergent = make_job(
        0,
        1,
        outcome=Outcome.FAILED,
        failure_kind=FailureKind.HARNESS,
        failure_mode="replay_divergence",
        matched_expectation=False,
    )
    escalated = make_job(0, 1, outcome=Outcome.ESCALATED)
    assert replay_passed(summarize("d", [make_trial(1, [make_job(0, 1)])]))
    assert not replay_passed(summarize("d", [make_trial(1, [divergent])]))
    assert not replay_passed(
        summarize("d", [make_trial(1, [make_job(0, 1)]), make_trial(2, [escalated])])
    )


def test_summarize_command_rebuilds_the_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(make_trial(1, [make_job(0, 1)]), trial_path(tmp_path, 1))
    assert main(["summarize", "--label", "demo", "--out", str(tmp_path)]) == 0
    assert summary_path(tmp_path).is_file()
    assert "demo: replay mode" in capsys.readouterr().out


def test_a_real_trial_can_keep_its_responses_apart_from_the_replay_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kept: dict[str, Any] = {}

    async def record(**arguments: Any) -> TrialRecord:
        kept.update(arguments)
        raise SystemExit(0)

    monkeypatch.setattr(bench.cli, "record_trial", record)
    monkeypatch.setattr(bench.cli, "get_settings", lambda: SimpleNamespace(groq_api_key="k"))
    argv = ["record", "--trial", "2", "--recordings-out", str(tmp_path / "rec")]
    argv += ["--extra-rule", "one", "--extra-rule", "two"]
    with pytest.raises(SystemExit):
        main(argv)
    assert kept["recordings_dir"] == tmp_path / "rec"
    assert kept["extra_rules"] == ["one", "two"]


def test_replay_command_rejects_more_than_one_worker() -> None:
    with pytest.raises(SystemExit):
        main(["replay", "--workers", "2"])


async def test_ab_trials_interleave_two_arms_that_differ_only_in_the_executor(
    tmp_path: Path, fleet_engine: AsyncEngine, fleet_database_url: str
) -> None:
    result = await ab_trials(
        taskset=MINI_TASKSET,
        recordings=await record_mini_batch(),
        latency=LatencyProfile.ZERO,
        trials=2,
        count=3,
        seed=1,
        out_root=tmp_path,
        first=SequentialExecutor(),
        second=FleetExecutor(fleet_database_url, lease_seconds=60),
        verbose=False,
    )
    assert result.outcomes_match
    assert result.first.trials == result.second.trials == [1, 2]
    assert replay_passed(result.first) and replay_passed(result.second)
    # what differs is where the jobs ran, and nothing about the tasks, recordings or agent
    assert _differing(result) == {"executor", "topology", "execution"}
    assert result.second.config.execution is not None
    assert result.second.config.execution["mode"] == "process"
    assert (tmp_path / "ab-fleet-1w-replay-zero" / "summary.json").is_file()
    assert (tmp_path / "ab-sequential-replay-zero" / "trial-2.json").is_file()


def test_trial_order_reverses_every_other_trial_so_no_arm_always_goes_first() -> None:
    assert trial_order(2, 1) == [0, 1]
    assert trial_order(2, 2) == [1, 0]
    assert trial_order(3, 2) == [2, 1, 0]
    # over a pair of trials every arm sits at the same average position
    positions = [trial_order(3, 1).index(arm) + trial_order(3, 2).index(arm) for arm in range(3)]
    assert positions == [2, 2, 2]


def _differing(result: AbResult) -> set[str]:
    return {
        field
        for field in BenchConfig.model_fields
        if getattr(result.first.config, field) != getattr(result.second.config, field)
    }


async def test_ab_trials_can_compare_the_fleet_in_process_against_in_containers(
    tmp_path: Path, fleet_engine: AsyncEngine, fleet_database_url: str, task_image: str
) -> None:
    containers = FleetExecutor(
        fleet_database_url, lease_seconds=60, mode="container", task_image=task_image
    )
    result = await ab_trials(
        taskset=MINI_TASKSET,
        recordings=await record_mini_batch(),
        latency=LatencyProfile.ZERO,
        trials=2,
        count=3,
        seed=1,
        out_root=tmp_path,
        first=FleetExecutor(fleet_database_url, lease_seconds=60),
        second=containers,
        verbose=False,
    )
    assert result.outcomes_match
    assert replay_passed(result.first) and replay_passed(result.second)
    assert _differing(result) == {"topology", "execution"}
    [trial] = [
        t for t in load_trials(tmp_path / "ab-fleet-1w-container-replay-zero") if t.trial == 1
    ]
    for job in trial.jobs:
        assert job.execution is not None and job.execution["mode"] == "container"
        assert job.execution["usage"]["max_rss_mb"] > 0


async def test_the_replay_command_runs_trials_on_a_pool_of_fleet_workers(
    tmp_path: Path, fleet_engine: AsyncEngine, fleet_database_url: str
) -> None:
    argv = [
        *("replay", "--executor", "fleet", "--workers", "2", "--trials", "1", "--tasks", "4"),
        *("--database-url", fleet_database_url, "--out", str(tmp_path)),
    ]
    # the command runs its own event loop, so it gets a thread of its own
    assert await asyncio.to_thread(main, argv) == 0
    [trial] = load_trials(tmp_path)
    assert (trial.label, trial.config.executor, trial.config.workers) == (
        "fleet-2w-replay-zero",
        "fleet",
        2,
    )
    assert "2 fleet worker processes" in trial.config.topology
    assert {job.worker for job in trial.jobs} <= {"w0", "w1"}


def test_the_scale_command_needs_distinct_worker_counts_of_at_least_one() -> None:
    for counts in (["2", "2"], ["0", "1"]):
        with pytest.raises(SystemExit):
            main(["scale", "--workers", *counts])


async def test_the_scale_command_interleaves_pools_that_differ_only_in_their_size(
    tmp_path: Path, fleet_engine: AsyncEngine, fleet_database_url: str
) -> None:
    argv = [
        *("scale", "--workers", "1", "2", "--trials", "2", "--tasks", "4"),
        *("--database-url", fleet_database_url, "--out-root", str(tmp_path)),
    ]
    # the command runs its own event loop, so it gets a thread of its own
    assert await asyncio.to_thread(main, argv) == 0
    one = load_trials(tmp_path / "scale-fleet-1w-replay-zero")
    two = load_trials(tmp_path / "scale-fleet-2w-replay-zero")
    assert [trial.trial for trial in one] == [trial.trial for trial in two] == [1, 2]
    differing = {
        field
        for field in BenchConfig.model_fields
        if getattr(one[0].config, field) != getattr(two[0].config, field)
    }
    assert differing == {"workers", "topology"}
    for trial in one + two:
        assert trial.resources is not None and trial.database is not None
        assert trial.database["publish"].calls == 4
    assert (tmp_path / "scale-fleet-2w-replay-zero" / "summary.json").is_file()
