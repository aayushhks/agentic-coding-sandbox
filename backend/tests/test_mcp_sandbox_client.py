"""Tests for the McpSandbox transport: it satisfies the Sandbox interface by driving the sandbox
MCP server over stdio, so tool calls behave the same as the in-process sandbox."""

from app.mcp.sandbox_client import McpSandbox
from app.sandbox.tools import ToolCall, ToolName


def test_write_and_read_through_mcp() -> None:
    sandbox = McpSandbox()
    try:
        write = ToolCall(ToolName.WRITE_FILE, {"path": "a.py", "content": "x = 1\n"})
        assert sandbox.execute(write).ok
        read = sandbox.execute(ToolCall(ToolName.READ_FILE, {"path": "a.py"}))
        assert read.ok
        assert read.output == "x = 1\n"
    finally:
        sandbox.cleanup()


def test_run_tests_through_mcp() -> None:
    sandbox = McpSandbox()
    try:
        test_body = "def test_ok():\n    assert True\n"
        sandbox.execute(ToolCall(ToolName.WRITE_FILE, {"path": "test_x.py", "content": test_body}))
        result = sandbox.execute(ToolCall(ToolName.RUN_TESTS, {}))
        assert result.ok
        assert result.exit_code == 0
    finally:
        sandbox.cleanup()


def test_finish_is_handled_locally() -> None:
    sandbox = McpSandbox()
    try:
        result = sandbox.execute(ToolCall(ToolName.FINISH, {"answer": "done"}))
        assert result.ok
        assert result.output == "done"
    finally:
        sandbox.cleanup()


def test_cleanup_removes_the_workspace() -> None:
    sandbox = McpSandbox()
    workspace = sandbox.workspace
    assert workspace.exists()
    sandbox.cleanup()
    assert not workspace.exists()
