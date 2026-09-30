import pytest

from app.agent.loop import Agent
from app.benchmark.runner import setup_workspace
from app.llm.mock_provider import MockProvider
from app.sandbox.subprocess_sandbox import SubprocessSandbox
from bench.executor import AGENT_CONFIGS
from bench.replay import RECORDINGS_ROOT, load_recordings, prefix_sha256
from bench.taskset import TASKSET_VERSION, BenchTask, load_taskset

TASKSET = load_taskset()
RECORDINGS = load_recordings(RECORDINGS_ROOT / TASKSET_VERSION)


def test_every_task_has_exactly_one_recording() -> None:
    assert set(RECORDINGS) == {task.id for task in TASKSET.tasks}


def test_recordings_come_from_one_model_on_this_task_set() -> None:
    assert {recording.taskset_version for recording in RECORDINGS.values()} == {TASKSET_VERSION}
    assert len({recording.model for recording in RECORDINGS.values()}) == 1


def _opening_prompt_hash(task: BenchTask) -> str:
    if task.benchmark is not None:
        files, text = task.benchmark.workspace_files, task.benchmark.description
    elif task.ticket is not None:
        files, text = task.ticket.workspace_files, task.ticket.body
    else:
        raise ValueError(f"task {task.id!r} has no payload")
    sandbox = SubprocessSandbox()
    try:
        setup_workspace(sandbox, files)
        agent = Agent(MockProvider([]), sandbox, AGENT_CONFIGS[task.kind])
        return prefix_sha256(agent._initial_messages(text))
    finally:
        sandbox.cleanup()


@pytest.mark.parametrize("task", TASKSET.tasks, ids=[task.id for task in TASKSET.tasks])
def test_recording_still_matches_the_agents_opening_prompt(task: BenchTask) -> None:
    # a prompt or tool change without a fresh recording would make replay meaningless
    recording = RECORDINGS[task.id]
    if not recording.calls:
        pytest.skip("the provider failed before answering, so there is no prompt to compare")
    assert recording.calls[0].prefix_sha256 == _opening_prompt_hash(task)
