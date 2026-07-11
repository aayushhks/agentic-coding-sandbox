"""Conformance tests for the issue-tracker MCP server: spawn it via the real MCP client over
stdio, complete the handshake, list tools, and exercise every tool with real assertions."""

from pathlib import Path

from tests.mcp_helpers import server_session, structured

_MODULE = "app.mcp.tracker_server"


async def test_lists_the_four_tools(tmp_path: Path) -> None:
    async with server_session("-m", _MODULE, "--tickets", str(tmp_path / "t.json")) as session:
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == {
            "list_tickets",
            "get_ticket",
            "update_ticket_status",
            "add_comment",
        }


async def test_lists_and_reads_seeded_tickets(tmp_path: Path) -> None:
    async with server_session("-m", _MODULE, "--tickets", str(tmp_path / "t.json")) as session:
        listed = await session.call_tool("list_tickets", {})
        assert not listed.isError
        assert len(structured(listed)["result"]) == 4
        one = await session.call_tool("get_ticket", {"ticket_id": "ACS-101"})
        assert structured(one)["id"] == "ACS-101"
        assert structured(one)["status"] == "open"


async def test_filters_by_status(tmp_path: Path) -> None:
    async with server_session("-m", _MODULE, "--tickets", str(tmp_path / "t.json")) as session:
        res = await session.call_tool("list_tickets", {"status": "open"})
        ids = {ticket["id"] for ticket in structured(res)["result"]}
        assert "ACS-101" in ids
        assert "ACS-104" not in ids  # ACS-104 is in_progress, not open


async def test_updates_status_and_adds_a_comment(tmp_path: Path) -> None:
    async with server_session("-m", _MODULE, "--tickets", str(tmp_path / "t.json")) as session:
        moved = await session.call_tool(
            "update_ticket_status", {"ticket_id": "ACS-101", "status": "resolved"}
        )
        assert structured(moved)["status"] == "resolved"
        commented = await session.call_tool(
            "add_comment", {"ticket_id": "ACS-101", "author": "agent", "body": "fixed it"}
        )
        comments = structured(commented)["comments"]
        assert len(comments) == 1
        assert comments[0]["author"] == "agent"


async def test_missing_ticket_is_an_error(tmp_path: Path) -> None:
    async with server_session("-m", _MODULE, "--tickets", str(tmp_path / "t.json")) as session:
        res = await session.call_tool("get_ticket", {"ticket_id": "NOPE"})
        assert res.isError
