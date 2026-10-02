import asyncio
import json
from pathlib import Path

import pytest

from bench.cli import main
from bench.compare import compare, mcnemar_p, render
from bench.jobs import Outcome
from bench.records import TrialRecord, trial_path, write_record
from tests.bench_helpers import make_config, make_environment, make_job, make_trial

RULE = "Keep the thought field to one short sentence."
TASKS = [f"t{n}" for n in range(6)]


def _arm(
    label: str,
    rounds: int = 3,
    *,
    tokens: int = 120,
    failing: frozenset[str] = frozenset(),
    config_overrides: dict[str, object] | None = None,
    sha: str = "a" * 40,
    interrupted: frozenset[int] = frozenset(),
) -> list[TrialRecord]:
    trials = []
    for round_ in range(1, rounds + 1):
        jobs = []
        for index, task in enumerate(TASKS):
            fails = task in failing
            jobs.append(
                make_job(
                    index,
                    index + 1,
                    job_id=f"{task}#0",
                    task_id=task,
                    prompt_tokens=tokens - 20,
                    completion_tokens=20,
                    cost_usd=tokens / 1e6,
                    outcome=Outcome.FAILED if fails else Outcome.SOLVED,
                    failure_mode="wrong_solution" if fails else None,
                    failure_kind="task" if fails else None,
                    matched_expectation=not fails,
                )
            )
        environment = make_environment().model_copy(update={"git_sha": sha})
        trials.append(
            make_trial(
                round_,
                jobs,
                label=label,
                config=make_config(**(config_overrides or {})),
                environment=environment,
                interrupted="stopped" if round_ in interrupted else None,
            )
        )
    return trials


def test_mcnemar_is_exact_and_two_sided() -> None:
    assert mcnemar_p(0, 0) == 1.0
    assert mcnemar_p(0, 6) == pytest.approx(2 / 64)
    assert mcnemar_p(6, 0) == pytest.approx(2 / 64)
    assert mcnemar_p(1, 5) == pytest.approx(2 * 7 / 64)
    assert mcnemar_p(3, 3) == 1.0


def test_two_runs_of_the_same_config_show_no_change() -> None:
    result = compare(_arm("a"), _arm("b"), resamples=500)
    assert (result.rounds, result.jobs, result.pairs) == ([1, 2, 3], 6, 18)
    assert result.config_changes == {} and result.prompt_diff == ""
    assert (result.matched.change, result.matched.low, result.matched.high) == (0, 0, 0)
    assert (result.better_pairs, result.worse_pairs, result.mcnemar_p) == (0, 0, 1.0)
    assert result.tasks == []
    assert all(line.endswith("no detectable change.") for line in result.verdict)


def test_a_change_that_saves_tokens_on_every_job_is_better_by_that_much() -> None:
    result = compare(_arm("a", tokens=120), _arm("b", tokens=100), resamples=500)
    tokens = result.tokens_per_job
    assert (tokens.baseline, tokens.candidate, tokens.change) == (120, 100, -20)
    assert tokens.low == tokens.high == -20
    assert result.cost_per_job is not None
    assert result.cost_per_job.change == pytest.approx(-20 / 1e6)
    [line] = [line for line in result.verdict if line.startswith("Tokens per job")]
    assert "-16.7%" in line and line.endswith("better.")


def test_outcome_flips_are_counted_per_paired_run_and_named_per_task() -> None:
    result = compare(_arm("a"), _arm("b", failing=frozenset({"t1", "t4"})), resamples=2_000)
    assert (result.better_pairs, result.worse_pairs) == (0, 6)
    assert result.mcnemar_p == pytest.approx(2 / 64)
    assert result.matched.change == pytest.approx(-2 / 6)
    assert [task.task_id for task in result.tasks] == ["t1", "t4"]
    moved = result.tasks[0]
    assert (moved.baseline, moved.candidate) == ({"solved": 3}, {"failed: wrong_solution": 3})
    assert (moved.baseline_matched, moved.candidate_matched) == (3, 0)
    # six flipped runs are only two tasks three times over: resampling jobs, a draw of six
    # without either of them is common enough that the interval reaches zero
    assert result.matched.low < 0 and result.matched.high == 0
    assert result.verdict[0].endswith("no detectable change.")


def test_flips_across_most_tasks_are_worse_even_resampling_jobs() -> None:
    failing = frozenset({"t0", "t1", "t3", "t4"})
    result = compare(_arm("a"), _arm("b", failing=failing), resamples=2_000)
    assert result.matched.change == pytest.approx(-4 / 6)
    assert result.matched.high < 0
    assert result.verdict[0].endswith("worse.")


def test_only_the_declared_change_may_differ_between_the_configs() -> None:
    prompts = {"benchmark": "You are an agent.\n- Rules."}
    ruled = {"benchmark": f"You are an agent.\n- Rules.\n- {RULE}"}
    baseline = _arm("a", config_overrides={"system_prompts": prompts, "agent_digest": "x"})
    candidate = _arm("b", config_overrides={"system_prompts": ruled, "agent_digest": "y"})
    result = compare(baseline, candidate, expect=["system_prompts", "agent_digest"], resamples=100)
    assert set(result.config_changes) == {"system_prompts", "agent_digest"}
    assert result.unexpected_changes == []
    assert f"+- {RULE}" in result.prompt_diff
    assert "```diff" in render(result)
    # the same runs, judged with only the digest declared, leave the prompts unexplained
    undeclared = compare(baseline, candidate, expect=["agent_digest"], resamples=100)
    assert undeclared.unexpected_changes == ["system_prompts"]


def test_only_rounds_both_runs_completed_are_paired() -> None:
    result = compare(_arm("a"), _arm("b", interrupted=frozenset({3})), resamples=100)
    assert result.rounds == [1, 2]
    assert result.unpaired == {"baseline": [3], "candidate": [3]}
    assert result.pairs == 12


def test_a_change_of_code_or_machine_between_the_runs_is_reported() -> None:
    result = compare(_arm("a"), _arm("b", sha="b" * 40), resamples=100)
    assert result.environment_changes == {"git_sha": [["a" * 40], ["b" * 40]]}


def test_runs_with_nothing_to_pair_are_refused() -> None:
    with pytest.raises(ValueError, match="no complete round"):
        compare(_arm("a", interrupted=frozenset({1, 2, 3})), _arm("b"))


async def test_the_compare_command_writes_its_record_and_fails_on_an_undeclared_change(
    tmp_path: Path,
) -> None:
    for name, trials in (
        ("base", _arm("a")),
        ("cand", _arm("b", tokens=100, config_overrides={"seed": 2})),
    ):
        for trial in trials:
            write_record(trial, trial_path(tmp_path / name, trial.trial))
    out = tmp_path / "comparison.json"
    argv = ["compare", "--baseline", str(tmp_path / "base"), "--candidate", str(tmp_path / "cand")]
    argv += ["--resamples", "100", "--out", str(out), "--markdown", str(tmp_path / "c.md")]
    assert await asyncio.to_thread(main, [*argv, "--expect", "seed"]) == 0
    record = json.loads(out.read_text())
    assert record["record"] == "comparison" and record["config_changes"] == {"seed": [1, 2]}
    assert (tmp_path / "c.md").read_text().startswith("# b against a")
    assert await asyncio.to_thread(main, [*argv, "--expect", "model"]) == 2
