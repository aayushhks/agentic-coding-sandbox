"""Data types describing an agent run and its individual steps."""

from dataclasses import dataclass
from enum import StrEnum

from app.sandbox.tools import ToolCall, ToolResult


class TerminationReason(StrEnum):
    FINISHED = "finished"
    MAX_ITERATIONS = "max_iterations"
    MALFORMED_LIMIT = "malformed_limit"
    PROVIDER_ERROR = "provider_error"


@dataclass(frozen=True, slots=True)
class AgentConfig:
    max_iterations: int = 15
    max_consecutive_malformed: int = 3
    temperature: float = 0.0
    max_tokens: int = 1024
    include_initial_listing: bool = True


@dataclass(slots=True)
class AgentStep:
    """One reason -> act -> observe cycle, captured for the trace viewer and eval."""

    index: int
    thought: str
    raw_response: str
    tool_call: ToolCall | None
    tool_result: ToolResult | None
    observation: str
    malformed: bool
    prompt_tokens: int
    completion_tokens: int


@dataclass(slots=True)
class AgentRun:
    """The full record of an agent solving (or failing) a single task."""

    steps: list[AgentStep]
    termination_reason: TerminationReason
    final_answer: str
    prompt_tokens: int
    completion_tokens: int

    @property
    def iterations(self) -> int:
        return len(self.steps)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def finished_cleanly(self) -> bool:
        return self.termination_reason == TerminationReason.FINISHED

    def tool_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for step in self.steps:
            if step.tool_call is not None:
                key = step.tool_call.name.value
                counts[key] = counts.get(key, 0) + 1
        return counts
