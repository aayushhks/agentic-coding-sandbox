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
        verified = False
        termination = TerminationReason.MAX_ITERATIONS
        final_answer = ""
        escalation_reason = ""

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

            if parsed.tool_call.name == ToolName.ESCALATE:
                escalation_reason = str(parsed.tool_call.arguments.get("reason", ""))
                steps.append(
                    AgentStep(
                        index=index,
                        thought=parsed.thought,
                        raw_response=raw,
                        tool_call=parsed.tool_call,
                        tool_result=None,
                        observation="ticket escalated to a human by the agent",
                        malformed=False,
                        prompt_tokens=completion.prompt_tokens,
                        completion_tokens=completion.completion_tokens,
                    )
                )
                termination = TerminationReason.ESCALATED
                break

            if parsed.tool_call.name == ToolName.FINISH:
                if self._config.require_verified_finish and not verified:
                    observation = (
                        "You cannot finish yet: write your own test file and run it with the "
                        "run_tests tool until the tests pass. A result of 'no tests ran' "
                        "(exit code 5) does not count as verification."
                    )
                    steps.append(
                        AgentStep(
                            index=index,
                            thought=parsed.thought,
                            raw_response=raw,
                            tool_call=parsed.tool_call,
                            tool_result=None,
                            observation=observation,
                            malformed=False,
                            prompt_tokens=completion.prompt_tokens,
                            completion_tokens=completion.completion_tokens,
                        )
                    )
                    messages.append(Message(role=Role.USER, content=observation))
                    continue
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
            if self._counts_as_verification(parsed.tool_call, result):
                verified = True
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
            escalation_reason=escalation_reason,
        )

    async def _execute(self, call: ToolCall) -> ToolResult:
        try:
            return await asyncio.to_thread(self._sandbox.execute, call)
        except Exception as exc:  # sandbox failures are surfaced to the agent as observations
            return ToolResult(output=f"sandbox error: {exc}", ok=False)

    @staticmethod
    def _counts_as_verification(call: ToolCall, result: ToolResult) -> bool:
        """True when a tool call actually ran tests and they passed.

        A run_tests (or a run_command invoking pytest) with exit code 0 means at least one test
        was collected and passed; pytest reports "no tests ran" as exit code 5, so that case is
        correctly excluded.
        """
        if result.exit_code != 0 or result.timed_out:
            return False
        if call.name == ToolName.RUN_TESTS:
            return True
        if call.name == ToolName.RUN_COMMAND:
            return "pytest" in str(call.arguments.get("command", ""))
        return False

    def _initial_messages(self, task: str) -> list[Message]:
        parts = [f"Task:\n{task}"]
        if self._config.include_initial_listing:
            listing = self._sandbox.execute(ToolCall(ToolName.LIST_DIR, {"path": "."}))
            parts.append(f"\nWorkspace root contents:\n{listing.output}")
        parts.append("\nReason about the task, then issue your first tool call.")
        return [
            Message(
                role=Role.SYSTEM,
                content=build_system_prompt(
                    self._config.require_verified_finish, self._config.allow_escalation
                ),
            ),
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
