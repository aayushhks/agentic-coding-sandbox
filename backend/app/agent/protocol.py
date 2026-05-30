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


def build_system_prompt() -> str:
    """Construct the system prompt describing the tools and required output format."""
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
        "- When the task is complete and its tests pass, call the finish tool.",
    ]
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
    """Pull the JSON object out of a response that may be fenced or wrapped in prose."""
    stripped = text.strip()
    if "```" in stripped:
        for block in stripped.split("```"):
            candidate = block[4:] if block.startswith("json") else block
            candidate = candidate.strip()
            if candidate.startswith("{") and candidate.endswith("}"):
                return candidate
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        return stripped[start : end + 1]
    return stripped


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
