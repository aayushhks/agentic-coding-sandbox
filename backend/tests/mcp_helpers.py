"""Helpers for driving the MCP servers over real stdio in tests."""

import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def server_session(*args: str) -> AsyncIterator[ClientSession]:
    """Spawn `python -m <args>` as an MCP server over stdio and yield an initialized session."""
    params = StdioServerParameters(
        command=sys.executable,
        args=list(args),
        cwd=str(_BACKEND_ROOT),
        env=dict(os.environ),
    )
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        yield session


def structured(result: CallToolResult) -> dict[str, Any]:
    """Return a tool call's structured content, asserting it is present."""
    assert result.structuredContent is not None, "expected structured content"
    return result.structuredContent
