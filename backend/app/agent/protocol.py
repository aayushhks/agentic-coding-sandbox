"""The ReAct tool-call protocol: system prompt, parsing, and observation formatting."""

import json
from dataclasses import dataclass

from app.sandbox.tools import TOOL_SPECS, ToolCall, ToolName, ToolResult

_VALID_TOOLS = {tool.value for tool in ToolName}


class ToolCallParseError(Exception):
    """Raised when the model's response is not a valid tool call."""


@dataclass(frozen=True, slots=True)
class ParsedStep:
    thought: str
    tool_call: ToolCall


def build_system_prompt(require_verified_finish: bool = False) -> str:
    """Construct the system prompt describing the tools and required output format.

    When ``require_verified_finish`` is set, the prompt tells the agent it must author and pass
    its own tests before finishing, matching the enforcement in the agent loop.
    """
    lines = [
        "You are an autonomous coding agent working inside a sandboxed workspace.",
        "You solve the task by issuing one tool call at a time and observing the result.",
        "",
        "Available tools:",
    ]
    for spec in TOOL_SPECS:
        params = ", ".join(f"{name} ({desc})" for name, desc in spec.parameters.items()) or "none"
        lines.append(f"- {spec.name.value}: {spec.description} arguments: {params}")
    lines += [
        "",
        "Respond with a SINGLE JSON object and nothing else, in this exact shape:",
        '{"thought": "<your reasoning>", "tool": "<tool name>", "arguments": {<arguments>}}',
        "",
        "Rules:",
        "- Emit exactly one tool call per response.",
        "- Use paths relative to the workspace root.",
        "- Inspect files and run the tests before declaring success.",
    ]
    if require_verified_finish:
        lines += [
            "- The workspace ships no tests: write your own test file covering the task, then run"
            " it with the run_tests tool.",
            '- Only call finish after run_tests reports passing tests. A result of "no tests ran"'
            " does not count as verification.",
        ]
    else:
        lines.append("- When the task is complete and its tests pass, call the finish tool.")
    return "\n".join(lines)


def parse_tool_call(text: str) -> ParsedStep:
    """Parse a model response into a tool call, raising ToolCallParseError if malformed."""
    try:
        data = json.loads(_extract_json(text))
    except json.JSONDecodeError as exc:
        raise ToolCallParseError(f"response was not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ToolCallParseError("response JSON must be an object")
    tool = data.get("tool")
    if tool not in _VALID_TOOLS:
        raise ToolCallParseError(f"unknown or missing tool: {tool!r}")
    arguments = data.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ToolCallParseError("'arguments' must be an object")
    return ParsedStep(
        thought=str(data.get("thought", "")),
        tool_call=ToolCall(name=ToolName(tool), arguments=arguments),
    )


def _extract_json(text: str) -> str:
    """Return the first complete JSON object from a response that may be fenced or wrapped in prose.

    Scans from the first ``{`` tracking brace depth (ignoring braces inside strings) and returns
    the slice that closes the first balanced object. Unlike a slice to the last ``}``, this is
    robust to trailing junk — a stray closing brace, a second object, or explanatory prose — which
    is a real failure mode of weaker models emitting large tool calls.
    """
    start = text.find("{")
    if start == -1:
        return text.strip()
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return text[start:]


def format_observation(result: ToolResult) -> str:
    """Render a tool result into the observation text appended to the conversation."""
    header = "[ok]" if result.ok else "[error]"
    if result.exit_code is not None:
        header += f" exit_code={result.exit_code}"
    if result.timed_out:
        header += " timed_out=true"
    if result.truncated:
        header += " truncated=true"
    return f"{header}\n{result.output}"
