import json
from typing import Any

from bench.jobs import Outcome
from bench.replay import LatencyProfile
from bench.runner import execution_from_body, replay_payload, run_job
from fleet.progress import reporting_to
from tests.bench_helpers import MINI_TASKSET, record_mini_batch


async def test_a_replay_payload_is_plain_json_and_runs_anywhere() -> None:
    recordings = await record_mini_batch()
    expected = {"adder": "succeeded", "TCK-04": "escalated", "unsatisfiable_spec": "failed"}
    for task in MINI_TASKSET.tasks:
        payload = replay_payload(task, recordings[task.id], LatencyProfile.ZERO)
        # it crosses the api as json, so it has to survive a round trip unchanged
        assert json.loads(json.dumps(payload)) == payload
        outcome = await run_job(task.id, payload)
        assert outcome.outcome == expected[task.id]
        execution = execution_from_body(outcome.body)
        assert execution.outcome.value == recordings[task.id].outcome
        assert execution.divergence is None


async def test_a_payload_carries_the_experiment_s_rules_to_the_worker() -> None:
    rule = "Keep the thought field to one short sentence."
    recordings = await record_mini_batch([rule])
    task = MINI_TASKSET.get("adder")
    ruled = replay_payload(task, recordings[task.id], LatencyProfile.ZERO, [rule])
    execution = execution_from_body((await run_job(task.id, ruled)).body)
    assert (execution.outcome, execution.divergence) == (Outcome.SOLVED, None)
    # the same recording without the rule meets a different prompt, and replay refuses it
    plain = replay_payload(task, recordings[task.id], LatencyProfile.ZERO)
    assert execution_from_body((await run_job(task.id, plain)).body).divergence is not None


async def test_the_runner_reports_what_the_recording_did() -> None:
    recordings = await record_mini_batch()
    task = MINI_TASKSET.get("unsatisfiable_spec")
    outcome = await run_job(task.id, replay_payload(task, recordings[task.id], LatencyProfile.ZERO))
    execution = execution_from_body(outcome.body)
    assert (execution.outcome, execution.failure_mode) == (Outcome.FAILED, "wrong_solution")
    assert execution.llm_calls == len(recordings[task.id].calls)
    assert execution.prompt_tokens == sum(call.prompt_tokens for call in recordings[task.id].calls)


async def test_the_runner_reports_each_agent_step_as_it_happens() -> None:
    recordings = await record_mini_batch()
    task = MINI_TASKSET.get("adder")
    events: list[dict[str, Any]] = []
    with reporting_to(events.append):
        outcome = await run_job(
            task.id, replay_payload(task, recordings[task.id], LatencyProfile.ZERO)
        )
    execution = execution_from_body(outcome.body)
    assert [event["step"] for event in events] == list(range(execution.iterations))
    assert sum(event["prompt_tokens"] for event in events) == execution.prompt_tokens
    assert events[-1]["tool"] == "finish"
