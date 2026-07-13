import json

import pytest

from app.agent.protocol import (
    ParsedStep,
    ToolCallParseError,
    build_system_prompt,
    format_observation,
    parse_tool_call,
)
from app.sandbox.tools import ToolName, ToolResult


def test_parse_plain_json() -> None:
    text = json.dumps(
        {"thought": "t", "tool": "write_file", "arguments": {"path": "a", "content": "b"}}
    )
    parsed = parse_tool_call(text)
    assert isinstance(parsed, ParsedStep)
    assert parsed.tool_call.name == ToolName.WRITE_FILE
    assert parsed.tool_call.arguments == {"path": "a", "content": "b"}
    assert parsed.thought == "t"


def test_parse_json_in_code_fence() -> None:
    inner = json.dumps({"thought": "t", "tool": "list_dir", "arguments": {}})
    parsed = parse_tool_call(f"Sure!\n```json\n{inner}\n```\n")
    assert parsed.tool_call.name == ToolName.LIST_DIR


def test_parse_json_with_surrounding_prose() -> None:
    text = 'My action: {"tool": "finish", "arguments": {"answer": "done"}} hope that helps'
    parsed = parse_tool_call(text)
    assert parsed.tool_call.name == ToolName.FINISH
    assert parsed.tool_call.arguments == {"answer": "done"}


def test_parse_rejects_non_json() -> None:
    with pytest.raises(ToolCallParseError):
        parse_tool_call("I will write a file now.")


def test_parse_rejects_unknown_tool() -> None:
    with pytest.raises(ToolCallParseError):
        parse_tool_call(json.dumps({"tool": "delete_everything", "arguments": {}}))


def test_parse_rejects_non_object_arguments() -> None:
    with pytest.raises(ToolCallParseError):
        parse_tool_call(json.dumps({"tool": "list_dir", "arguments": "nope"}))


def test_parse_defaults_missing_arguments() -> None:
    parsed = parse_tool_call(json.dumps({"tool": "finish"}))
    assert parsed.tool_call.arguments == {}


def test_parse_ignores_trailing_stray_brace() -> None:
    # the real m6 lru_cache failure: a complete tool call followed by one extra '}'
    valid = json.dumps(
        {"thought": "t", "tool": "write_file", "arguments": {"path": "a.py", "content": "x = 1"}}
    )
    parsed = parse_tool_call(valid + "}")
    assert parsed.tool_call.name == ToolName.WRITE_FILE
    assert parsed.tool_call.arguments == {"path": "a.py", "content": "x = 1"}


def test_parse_handles_braces_inside_strings() -> None:
    code = "def f():\n    return {}\n"
    text = json.dumps({"tool": "write_file", "arguments": {"path": "a.py", "content": code}})
    parsed = parse_tool_call(text)
    assert parsed.tool_call.arguments == {"path": "a.py", "content": code}


def test_parse_takes_first_of_two_objects() -> None:
    first = json.dumps({"tool": "finish", "arguments": {"answer": "done"}})
    second = json.dumps({"tool": "list_dir", "arguments": {}})
    parsed = parse_tool_call(f"{first} {second}")
    assert parsed.tool_call.name == ToolName.FINISH


def test_system_prompt_lists_core_tools() -> None:
    prompt = build_system_prompt()
    for tool in ToolName:
        if tool == ToolName.ESCALATE:
            assert tool.value not in prompt  # escalate is opt-in, off by default
        else:
            assert tool.value in prompt


def test_system_prompt_offers_escalate_when_enabled() -> None:
    assert "escalate" in build_system_prompt(allow_escalation=True)


def test_format_observation_ok() -> None:
    obs = format_observation(ToolResult(output="hello", ok=True, exit_code=0))
    assert "[ok]" in obs
    assert "exit_code=0" in obs
    assert "hello" in obs


def test_format_observation_error_and_timeout() -> None:
    obs = format_observation(ToolResult(output="boom", ok=False, timed_out=True))
    assert "[error]" in obs
    assert "timed_out=true" in obs
