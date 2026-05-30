from collections.abc import Sequence
from typing import cast

from groq import AsyncGroq
from groq.types.chat import ChatCompletionMessageParam

from app.llm.base import CompletionResult, LLMProvider, Message


class GroqProvider(LLMProvider):
    """LLM provider backed by Groq's OpenAI-compatible chat completions API."""

    def __init__(self, api_key: str, *, model: str = "llama-3.3-70b-versatile") -> None:
        if not api_key:
            raise ValueError("groq api key must not be empty")
        self._client = AsyncGroq(api_key=api_key)
        self._model = model

    @property
    def name(self) -> str:
        return "groq"

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> CompletionResult:
        # groq's message params use Literal roles; our Role enum guarantees valid values,
        # so we build plain dicts and cast to the SDK's typed-dict union.
        groq_messages = cast(
            list[ChatCompletionMessageParam],
            [{"role": m.role.value, "content": m.content} for m in messages],
        )
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=groq_messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        choice = response.choices[0]
        content = choice.message.content or ""
        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        return CompletionResult(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
