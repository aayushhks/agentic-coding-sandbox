from app.sandbox.tools import TOOL_SPECS, ToolCall, ToolName, ToolResult, ToolSpec


def test_tool_specs_cover_every_tool() -> None:
    assert {spec.name for spec in TOOL_SPECS} == set(ToolName)


def test_tool_specs_are_well_formed() -> None:
    for spec in TOOL_SPECS:
        assert isinstance(spec, ToolSpec)
        assert spec.description
        assert isinstance(spec.parameters, dict)


def test_tool_call_defaults_to_empty_arguments() -> None:
    call = ToolCall(name=ToolName.LIST_DIR)
    assert call.arguments == {}


def test_tool_result_defaults() -> None:
    result = ToolResult(output="ok", ok=True, exit_code=0)
    assert result.ok
    assert result.exit_code == 0
    assert not result.timed_out
    assert not result.truncated
