"""Record real LLM responses once, then replay them deterministically and for free."""

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

from pydantic import BaseModel

from app.llm.base import CompletionResult, LLMProvider, Message

RECORDING_SCHEMA_VERSION = 1
RECORDINGS_ROOT = Path(__file__).resolve().parent / "recordings"

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
# seconds to wait before retrying a failed call, or None when retrying cannot help
RetryDelay = Callable[[Exception], float | None]


class LatencyProfile(StrEnum):
    ZERO = "zero"  # answer instantly: stresses the harness itself
    RECORDED = "recorded"  # wait each call's measured latency: approximates the real workload


class RecordedCall(BaseModel):
    index: int
    # hash of the system prompt and task message, checked on the first call only
    prefix_sha256: str | None
    # first line of the observation this call answered: status and exit code, never timings
    observation_head: str
    temperature: float
    max_tokens: int
    content: str
    prompt_tokens: int
    completion_tokens: int
    latency_seconds: float
    retry_wait_seconds: float


class Recording(BaseModel):
    schema_version: int = RECORDING_SCHEMA_VERSION
    task_id: str
    taskset_version: str
    provider: str
    model: str
    recorded_at: str
    git_sha: str
    outcome: str
    calls: list[RecordedCall]


def prefix_sha256(messages: Sequence[Message]) -> str:
    """Hash of the system prompt and the task message, which are fixed for a given task."""
    head = [{"role": m.role.value, "content": m.content} for m in messages[:2]]
    return hashlib.sha256(json.dumps(head, sort_keys=True).encode()).hexdigest()


def observation_head(messages: Sequence[Message]) -> str:
    """First line of the latest observation, or "" for the opening call."""
    if len(messages) <= 2:
        return ""
    return messages[-1].content.split("\n", 1)[0][:200]


def _never_retry(_exc: Exception) -> float | None:
    return None


class RecordingProvider(LLMProvider):
    """Wraps a real provider, timing each call and keeping every response it returns."""

    def __init__(
        self,
        inner: LLMProvider,
        *,
        retry_delay: RetryDelay = _never_retry,
        max_attempts: int = 8,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._inner = inner
        self._retry_delay = retry_delay
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._clock = clock
        self.calls: list[RecordedCall] = []
        self.gave_up: Exception | None = None

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def model(self) -> str:
        return self._inner.model

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> CompletionResult:
        first_attempt = self._clock()
        for attempt in range(1, self._max_attempts + 1):
            started = self._clock()
            try:
                result = await self._inner.complete(
                    messages, temperature=temperature, max_tokens=max_tokens
                )
            except Exception as exc:
                delay = self._retry_delay(exc)
                if delay is None or attempt == self._max_attempts:
                    self.gave_up = exc
                    raise
                await self._sleep(delay)
                continue
            self.calls.append(
                RecordedCall(
                    index=len(self.calls),
                    prefix_sha256=None if self.calls else prefix_sha256(messages),
                    observation_head=observation_head(messages),
                    temperature=temperature,
                    max_tokens=max_tokens,
                    content=result.content,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    latency_seconds=self._clock() - started,
                    retry_wait_seconds=started - first_attempt,
                )
            )
            return result
        raise AssertionError("the retry loop always returns or raises")


def build_recording(
    provider: RecordingProvider,
    *,
    task_id: str,
    taskset_version: str,
    outcome: str,
    git_sha: str,
    recorded_at: str,
) -> Recording:
    return Recording(
        task_id=task_id,
        taskset_version=taskset_version,
        provider=provider.name,
        model=provider.model,
        recorded_at=recorded_at,
        git_sha=git_sha,
        outcome=outcome,
        calls=list(provider.calls),
    )


class ReplayDivergenceError(RuntimeError):
    """The run asked for something its recording cannot answer."""


class ReplayProvider(LLMProvider):
    """Serves a recording's responses in order, refusing any request the recording did not see."""

    def __init__(
        self,
        recording: Recording,
        *,
        latency: LatencyProfile = LatencyProfile.ZERO,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._recording = recording
        self._latency = latency
        self._sleep = sleep
        self._cursor = 0
        self.divergence: str | None = None

    @property
    def name(self) -> str:
        return "replay"

    @property
    def model(self) -> str:
        return self._recording.model

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> CompletionResult:
        call = self._expect(messages, temperature, max_tokens)
        if self._latency == LatencyProfile.RECORDED:
            await self._sleep(call.latency_seconds)
        return CompletionResult(
            content=call.content,
            prompt_tokens=call.prompt_tokens,
            completion_tokens=call.completion_tokens,
        )

    def _expect(
        self, messages: Sequence[Message], temperature: float, max_tokens: int
    ) -> RecordedCall:
        calls = self._recording.calls
        index = self._cursor
        if index >= len(calls):
            self._diverge(f"call {index} was requested but only {len(calls)} were recorded")
        call = calls[index]
        if (temperature, max_tokens) != (call.temperature, call.max_tokens):
            self._diverge(
                f"call {index} used temperature={temperature} max_tokens={max_tokens}, "
                f"recorded temperature={call.temperature} max_tokens={call.max_tokens}"
            )
        if call.prefix_sha256 is not None and call.prefix_sha256 != prefix_sha256(messages):
            self._diverge("the system prompt or task message changed since the recording")
        head = observation_head(messages)
        if head != call.observation_head:
            self._diverge(
                f"call {index} answers {head!r} but the recording answered "
                f"{call.observation_head!r}"
            )
        self._cursor += 1
        return call

    def _diverge(self, reason: str) -> NoReturn:
        self.divergence = reason
        raise ReplayDivergenceError(reason)

    def check_consumed(self) -> None:
        """Flag a run that stopped before using every recorded call."""
        total = len(self._recording.calls)
        if self.divergence is None and self._cursor < total:
            self.divergence = f"the run stopped after {self._cursor} of {total} recorded calls"


def write_recording(recording: Recording, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{recording.task_id}.json"
    path.write_text(recording.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def load_recordings(directory: Path) -> dict[str, Recording]:
    loaded = [
        Recording.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.json"))
    ]
    return {recording.task_id: recording for recording in loaded}


def recordings_digest(recordings: dict[str, Recording]) -> str:
    """A content hash of a recording set, so a replay record pins exactly what it replayed."""
    payload = [recordings[task_id].model_dump(mode="json") for task_id in sorted(recordings)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
