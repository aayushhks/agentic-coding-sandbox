"""Conformance tests for the sandbox MCP server: spawn it via the real MCP client over stdio,
complete the handshake, list tools, and exercise the file and command tools with real assertions."""

from pathlib import Path

from tests.mcp_helpers import server_session, structured

_MODULE = "app.mcp.sandbox_server"


async def test_lists_the_five_tools(tmp_path: Path) -> None:
    async with server_session("-m", _MODULE, "--workspace", str(tmp_path)) as session:
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == {
            "read_file",
            "write_file",
            "list_dir",
            "run_command",
            "run_tests",
        }


async def test_write_then_read_roundtrips(tmp_path: Path) -> None:
    async with server_session("-m", _MODULE, "--workspace", str(tmp_path)) as session:
        wrote = await session.call_tool("write_file", {"path": "a.py", "content": "x = 1\n"})
        assert structured(wrote)["ok"]
        read = await session.call_tool("read_file", {"path": "a.py"})
        assert structured(read)["ok"]
        assert structured(read)["output"] == "x = 1\n"


async def test_list_dir_shows_written_files(tmp_path: Path) -> None:
    async with server_session("-m", _MODULE, "--workspace", str(tmp_path)) as session:
        await session.call_tool("write_file", {"path": "a.py", "content": "x = 1\n"})
        listed = await session.call_tool("list_dir", {"path": "."})
        assert "a.py" in structured(listed)["output"]


async def test_run_command_returns_output_and_exit_code(tmp_path: Path) -> None:
    async with server_session("-m", _MODULE, "--workspace", str(tmp_path)) as session:
        res = await session.call_tool("run_command", {"command": "echo hello"})
        sc = structured(res)
        assert sc["ok"]
        assert sc["exit_code"] == 0
        assert "hello" in sc["output"]
