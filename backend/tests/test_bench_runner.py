import json
from typing import Any

from bench.jobs import JobOutput, Outcome
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
    # the model that answered each recorded call, as its provider reported it
    assert (execution.served_models, execution.fingerprints) == (["mock-model"], [])


async def test_each_job_keeps_what_the_agent_left_and_what_the_grader_said() -> None:
    recordings = await record_mini_batch()

    async def output(task_id: str) -> JobOutput:
        task = MINI_TASKSET.get(task_id)
        payload = replay_payload(task, recordings[task_id], LatencyProfile.ZERO)
        execution = execution_from_body((await run_job(task_id, payload)).body)
        assert execution.output is not None
        return execution.output

    adder = await output("adder")
    assert adder.answer == "done"
    assert adder.files_changed == ["add.py", "test_add.py"]
    assert "+++ b/add.py" in adder.diff and "+    return a + b" in adder.diff
    # the hidden test file the grader writes afterwards is no change of the agent's
    assert "test_hidden_add" not in adder.diff
    assert (adder.tests["passed"], adder.tests["exit_code"]) == (True, 0)
    assert "1 passed" in adder.tests["output"]
    assert adder.tools == {"write_file": 2, "run_tests": 1, "finish": 1}
    assert adder.model_seconds == 0.0

    ticket = await output("TCK-04")
    assert ticket.escalation_reason == "no measurable target for 'faster'"
    assert ticket.files_changed == []
    # an escalated ticket never reaches the hidden tests, and its files were left alone
    assert ticket.tests["passed"] is None and ticket.tests["canaries_intact"] is True

    failing = await output("unsatisfiable_spec")
    assert failing.tests["passed"] is False and failing.tests["exit_code"] != 0


async def test_a_job_on_the_recorded_profile_counts_the_model_time_it_waited() -> None:
    recordings = await record_mini_batch()
    task = MINI_TASKSET.get("adder")
    payload = replay_payload(task, recordings["adder"], LatencyProfile.RECORDED)
    execution = execution_from_body((await run_job(task.id, payload)).body)
    assert execution.output is not None
    waited = sum(call.latency_seconds for call in recordings["adder"].calls)
    assert execution.output.model_seconds == round(waited, 3)


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
