from collections.abc import Sequence
from pathlib import Path

import pytest

from app.llm.base import CompletionResult, LLMProvider, Message, Role
from app.llm.mock_provider import MockProvider
from bench.replay import (
    LatencyProfile,
    RecordedCall,
    Recording,
    RecordingProvider,
    ReplayDivergenceError,
    ReplayProvider,
    build_recording,
    load_recordings,
    prefix_sha256,
    recordings_digest,
    write_recording,
)


def _conversation(*observations: str, system: str = "system prompt") -> list[Message]:
    messages = [Message(Role.SYSTEM, system), Message(Role.USER, "Task: do it")]
    for observation in observations:
        messages += [Message(Role.ASSISTANT, "{}"), Message(Role.USER, observation)]
    return messages


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FlakyError(Exception):
    pass


class FlakyProvider(LLMProvider):
    """Fails a set number of times before answering; every attempt takes half a fake second."""

    def __init__(self, time: FakeTime, failures: int) -> None:
        self._time = time
        self._failures = failures

    @property
    def name(self) -> str:
        return "flaky"

    @property
    def model(self) -> str:
        return "flaky-model"

    async def complete(
        self, messages: Sequence[Message], *, temperature: float = 0.0, max_tokens: int = 1024
    ) -> CompletionResult:
        self._time.now += 0.5
        if self._failures > 0:
            self._failures -= 1
            raise FlakyError("busy")
        return CompletionResult(content="answer", prompt_tokens=10, completion_tokens=2)


def _retry_flaky(exc: Exception) -> float | None:
    return 1.0 if isinstance(exc, FlakyError) else None


async def _record(responses: list[str], conversations: list[list[Message]]) -> Recording:
    provider = RecordingProvider(MockProvider(responses))
    for messages in conversations:
        await provider.complete(messages)
    return build_recording(
        provider,
        task_id="demo",
        taskset_version="v1",
        outcome="solved",
        git_sha="abc",
        recorded_at="2026-09-30T00:00:00+00:00",
    )


def _one_call(**overrides: object) -> Recording:
    fields: dict[str, object] = {
        "index": 0,
        "prefix_sha256": prefix_sha256(_conversation()),
        "observation_head": "",
        "temperature": 0.0,
        "max_tokens": 1024,
        "content": "first",
        "prompt_tokens": 5,
        "completion_tokens": 1,
        "latency_seconds": 0.25,
        "retry_wait_seconds": 0.0,
    }
    fields.update(overrides)
    return Recording(
        task_id="demo",
        taskset_version="v1",
        provider="groq",
        model="m",
        recorded_at="t",
        git_sha="abc",
        outcome="solved",
        calls=[RecordedCall.model_validate(fields)],
    )


async def test_recording_keeps_responses_tokens_and_request_fingerprints() -> None:
    first, second = _conversation(), _conversation("[ok] exit_code=0\n1 passed in 0.02s")
    recording = await _record(["a", "b"], [first, second])
    calls = recording.calls
    assert [call.content for call in calls] == ["a", "b"]
    assert calls[0].prefix_sha256 == prefix_sha256(first)
    assert calls[1].prefix_sha256 is None
    assert [call.observation_head for call in calls] == ["", "[ok] exit_code=0"]
    assert calls[1].prompt_tokens == sum(len(m.content.split()) for m in second)
    assert recording.provider == "mock"


async def test_recording_times_the_answer_apart_from_retry_waits() -> None:
    time = FakeTime()
    provider = RecordingProvider(
        FlakyProvider(time, failures=2),
        retry_delay=_retry_flaky,
        sleep=time.sleep,
        clock=time.clock,
    )
    await provider.complete(_conversation())
    call = provider.calls[0]
    assert call.latency_seconds == 0.5
    assert call.retry_wait_seconds == 3.0
    assert time.slept == [1.0, 1.0]


async def test_recording_gives_up_on_an_error_retrying_cannot_fix() -> None:
    time = FakeTime()
    provider = RecordingProvider(
        FlakyProvider(time, failures=1), sleep=time.sleep, clock=time.clock
    )
    with pytest.raises(FlakyError):
        await provider.complete(_conversation())
    assert isinstance(provider.gave_up, FlakyError)
    assert provider.calls == []


async def test_recording_gives_up_after_max_attempts() -> None:
    time = FakeTime()
    provider = RecordingProvider(
        FlakyProvider(time, failures=10),
        retry_delay=_retry_flaky,
        max_attempts=3,
        sleep=time.sleep,
        clock=time.clock,
    )
    with pytest.raises(FlakyError):
        await provider.complete(_conversation())
    assert time.slept == [1.0, 1.0]


async def test_replay_serves_a_written_recording_in_order(tmp_path: Path) -> None:
    conversations = [_conversation(), _conversation("[ok]\nwrote 3 bytes")]
    write_recording(await _record(["a", "b"], conversations), tmp_path)
    replay = ReplayProvider(load_recordings(tmp_path)["demo"])
    answers = [await replay.complete(messages) for messages in conversations]
    assert [answer.content for answer in answers] == ["a", "b"]
    replay.check_consumed()
    assert replay.divergence is None


async def test_replay_diverges_when_asked_for_an_unrecorded_call() -> None:
    replay = ReplayProvider(_one_call())
    await replay.complete(_conversation())
    with pytest.raises(ReplayDivergenceError, match="only 1 were recorded"):
        await replay.complete(_conversation("[ok]"))
    assert replay.divergence is not None


async def test_replay_diverges_when_the_prompt_changed() -> None:
    replay = ReplayProvider(_one_call())
    with pytest.raises(ReplayDivergenceError, match="changed since the recording"):
        await replay.complete(_conversation(system="a different system prompt"))


async def test_replay_diverges_when_an_observation_differs() -> None:
    recording = _one_call(index=0, prefix_sha256=None, observation_head="[ok] exit_code=0")
    replay = ReplayProvider(recording)
    with pytest.raises(ReplayDivergenceError, match="exit_code=1"):
        await replay.complete(_conversation("[error] exit_code=1\nFAILED"))


async def test_replay_diverges_when_sampling_settings_differ() -> None:
    replay = ReplayProvider(_one_call())
    with pytest.raises(ReplayDivergenceError, match="max_tokens"):
        await replay.complete(_conversation(), max_tokens=2048)


async def test_replay_flags_a_run_that_stops_early() -> None:
    recording = await _record(["a", "b"], [_conversation(), _conversation("[ok]")])
    replay = ReplayProvider(recording)
    await replay.complete(_conversation())
    replay.check_consumed()
    assert replay.divergence == "the run stopped after 1 of 2 recorded calls"


async def test_recorded_latency_profile_waits_the_measured_latency() -> None:
    time = FakeTime()
    recorded = ReplayProvider(_one_call(), latency=LatencyProfile.RECORDED, sleep=time.sleep)
    await recorded.complete(_conversation())
    assert time.slept == [0.25]
    instant = ReplayProvider(_one_call(), latency=LatencyProfile.ZERO, sleep=time.sleep)
    await instant.complete(_conversation())
    assert time.slept == [0.25]


def test_recordings_digest_changes_with_any_response() -> None:
    original = {"demo": _one_call()}
    edited = {"demo": _one_call(content="second")}
    assert recordings_digest(original) == recordings_digest({"demo": _one_call()})
    assert recordings_digest(original) != recordings_digest(edited)
