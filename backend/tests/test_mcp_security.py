"""The MCP trust boundary: file paths are workspace-relative only, escapes are refused, and
malformed input is a validation error at the boundary rather than a crash or a filesystem escape."""

from pathlib import Path

from tests.mcp_helpers import server_session, structured

_MODULE = "app.mcp.sandbox_server"


async def test_absolute_read_path_is_rejected(tmp_path: Path) -> None:
    async with server_session("-m", _MODULE, "--workspace", str(tmp_path)) as session:
        res = await session.call_tool("read_file", {"path": "/etc/passwd"})
        assert res.isError


async def test_absolute_write_path_cannot_escape(tmp_path: Path) -> None:
    target = tmp_path / "escape.txt"  # absolute, outside the workspace root below
    async with server_session("-m", _MODULE, "--workspace", str(tmp_path / "ws")) as session:
        res = await session.call_tool("write_file", {"path": str(target), "content": "pwned"})
        assert res.isError
    assert not target.exists()  # the write never landed


async def test_relative_escape_is_rejected(tmp_path: Path) -> None:
    async with server_session("-m", _MODULE, "--workspace", str(tmp_path / "ws")) as session:
        res = await session.call_tool("read_file", {"path": "../../../../etc/passwd"})
        assert not structured(res)["ok"]
        assert "escapes the workspace" in structured(res)["output"]


async def test_missing_required_argument_is_a_validation_error(tmp_path: Path) -> None:
    async with server_session("-m", _MODULE, "--workspace", str(tmp_path)) as session:
        res = await session.call_tool("read_file", {})  # 'path' is required
        assert res.isError
