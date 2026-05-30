from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class Message:
    """A single chat message handed to a provider."""

    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """The text a provider produced, plus token accounting for the eval harness."""

    content: str
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMProvider(ABC):
    """Model-agnostic interface every provider implements.

    Keeping the agent behind this abstraction lets the eval harness swap models
    (Groq for real runs, a scripted mock for deterministic tests) without touching
    the agent loop.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short provider identifier, e.g. "groq" or "mock"."""

    @property
    @abstractmethod
    def model(self) -> str:
        """The concrete model name this provider is configured to use."""

    @abstractmethod
    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> CompletionResult:
        """Produce a completion for the given conversation."""
