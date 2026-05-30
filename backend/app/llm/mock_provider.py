from collections.abc import Sequence

from app.llm.base import CompletionResult, LLMProvider, Message


class MockProvider(LLMProvider):
    """A deterministic provider that replays scripted responses in order.

    Used for free, reproducible tests of the agent loop without calling a real model.
    Token counts are word-count approximations, which is enough for harness accounting
    in tests.
    """

    def __init__(self, responses: Sequence[str], *, model: str = "mock-model") -> None:
        self._responses = list(responses)
        self._model = model
        self._cursor = 0

    @property
    def name(self) -> str:
        return "mock"

    @property
    def model(self) -> str:
        return self._model

    @property
    def calls(self) -> int:
        """How many times complete() has been called so far."""
        return self._cursor

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> CompletionResult:
        if self._cursor >= len(self._responses):
            raise IndexError(f"mock provider exhausted after {len(self._responses)} responses")
        content = self._responses[self._cursor]
        self._cursor += 1
        prompt_tokens = sum(len(m.content.split()) for m in messages)
        completion_tokens = len(content.split())
        return CompletionResult(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
