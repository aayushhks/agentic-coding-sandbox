"""The ReAct agent loop: reason, act via a tool call, observe, repeat."""

import asyncio

from app.agent.protocol import (
    ToolCallParseError,
    build_system_prompt,
    format_observation,
    parse_tool_call,
)
from app.agent.types import AgentConfig, AgentRun, AgentStep, TerminationReason
from app.llm.base import CompletionResult, LLMProvider, Message, Role
from app.sandbox.base import Sandbox
from app.sandbox.tools import ToolCall, ToolName, ToolResult


class Agent:
    """Drives an LLM through a reason -> act -> observe loop against a sandbox."""

    def __init__(
        self,
        provider: LLMProvider,
        sandbox: Sandbox,
        config: AgentConfig | None = None,
    ) -> None:
        self._provider = provider
        self._sandbox = sandbox
        self._config = config or AgentConfig()

    async def run(self, task: str) -> AgentRun:
        messages = self._initial_messages(task)
        steps: list[AgentStep] = []
        prompt_tokens = 0
        completion_tokens = 0
        consecutive_malformed = 0
        termination = TerminationReason.MAX_ITERATIONS
        final_answer = ""

        for index in range(self._config.max_iterations):
            try:
                completion = await self._provider.complete(
                    messages,
                    temperature=self._config.temperature,
                    max_tokens=self._config.max_tokens,
                )
            except Exception as exc:  # provider failures are a recorded outcome, not a crash
                steps.append(self._error_step(index, f"provider error: {exc}"))
                termination = TerminationReason.PROVIDER_ERROR
                break

            prompt_tokens += completion.prompt_tokens
            completion_tokens += completion.completion_tokens
            raw = completion.content
            messages.append(Message(role=Role.ASSISTANT, content=raw))

            try:
                parsed = parse_tool_call(raw)
            except ToolCallParseError as exc:
                consecutive_malformed += 1
                observation = (
                    f"Your last response could not be parsed: {exc}. Respond with a single "
                    'JSON object: {"thought": "...", "tool": "...", "arguments": {...}}.'
                )
                steps.append(self._malformed_step(index, raw, observation, completion))
                messages.append(Message(role=Role.USER, content=observation))
                if consecutive_malformed >= self._config.max_consecutive_malformed:
                    termination = TerminationReason.MALFORMED_LIMIT
                    break
                continue

            consecutive_malformed = 0

            if parsed.tool_call.name == ToolName.FINISH:
                final_answer = str(parsed.tool_call.arguments.get("answer", ""))
                steps.append(
                    AgentStep(
                        index=index,
                        thought=parsed.thought,
                        raw_response=raw,
                        tool_call=parsed.tool_call,
                        tool_result=None,
                        observation="task finished by agent",
                        malformed=False,
                        prompt_tokens=completion.prompt_tokens,
                        completion_tokens=completion.completion_tokens,
                    )
                )
                termination = TerminationReason.FINISHED
                break

            result = await self._execute(parsed.tool_call)
            observation = format_observation(result)
            steps.append(
                AgentStep(
                    index=index,
                    thought=parsed.thought,
                    raw_response=raw,
                    tool_call=parsed.tool_call,
                    tool_result=result,
                    observation=observation,
                    malformed=False,
                    prompt_tokens=completion.prompt_tokens,
                    completion_tokens=completion.completion_tokens,
                )
            )
            messages.append(Message(role=Role.USER, content=observation))

        return AgentRun(
            steps=steps,
            termination_reason=termination,
            final_answer=final_answer,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    async def _execute(self, call: ToolCall) -> ToolResult:
        try:
            return await asyncio.to_thread(self._sandbox.execute, call)
        except Exception as exc:  # sandbox failures are surfaced to the agent as observations
            return ToolResult(output=f"sandbox error: {exc}", ok=False)

    def _initial_messages(self, task: str) -> list[Message]:
        parts = [f"Task:\n{task}"]
        if self._config.include_initial_listing:
            listing = self._sandbox.execute(ToolCall(ToolName.LIST_DIR, {"path": "."}))
            parts.append(f"\nWorkspace root contents:\n{listing.output}")
        parts.append("\nReason about the task, then issue your first tool call.")
        return [
            Message(role=Role.SYSTEM, content=build_system_prompt()),
            Message(role=Role.USER, content="\n".join(parts)),
        ]

    @staticmethod
    def _error_step(index: int, observation: str) -> AgentStep:
        return AgentStep(
            index=index,
            thought="",
            raw_response="",
            tool_call=None,
            tool_result=None,
            observation=observation,
            malformed=False,
            prompt_tokens=0,
            completion_tokens=0,
        )

    @staticmethod
    def _malformed_step(
        index: int,
        raw: str,
        observation: str,
        completion: CompletionResult,
    ) -> AgentStep:
        return AgentStep(
            index=index,
            thought="",
            raw_response=raw,
            tool_call=None,
            tool_result=None,
            observation=observation,
            malformed=True,
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
        )
