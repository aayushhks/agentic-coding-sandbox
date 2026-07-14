"""Connect a real MCP client to the sandbox server and print a session transcript.

A screenshot-free, reproducible demonstration that any MCP client can discover and drive the
sandbox server's tools over stdio. Regenerate the committed transcript with:

    uv run --project backend python scripts/mcp_session_demo.py > docs/mcp-session.md
"""

import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_BACKEND = Path(__file__).resolve().parents[1] / "backend"

_HEADER = """# MCP session transcript

A real session: an MCP SDK client connected to `app.mcp.sandbox_server` over stdio, completed the
handshake, listed its tools, and called them — including the absolute path the workspace jail
rejects. This is the reproducible, screenshot-free demonstration linked from the README.

Regenerate with `uv run --project backend python scripts/mcp_session_demo.py > docs/mcp-session.md`.

```text"""


def _structured(result: Any) -> dict[str, Any]:
    return getattr(result, "structuredContent", None) or {}


def _first_text(result: Any) -> str:
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            return text.splitlines()[0]
    return ""


async def main() -> None:
    workspace = Path(tempfile.mkdtemp(prefix="acs-demo-"))
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp.sandbox_server", "--workspace", str(workspace)],
        cwd=str(_BACKEND),
    )
    lines: list[str] = [_HEADER]

    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        init = await session.initialize()
        lines.append("client -> server: initialize")
        lines.append(
            f"server -> client: ready  (server={init.serverInfo.name!r}, "
            f"protocol={init.protocolVersion})"
        )
        lines.append("")

        tools = await session.list_tools()
        lines.append("client -> server: list_tools")
        lines.append("server -> client: " + ", ".join(sorted(t.name for t in tools.tools)))
        lines.append("")

        wrote = await session.call_tool(
            "write_file", {"path": "hello.py", "content": 'print("hello from the sandbox")\n'}
        )
        lines.append('client -> server: call write_file {path: "hello.py", content: "print(...)"}')
        lines.append(f"server -> client: ok={_structured(wrote).get('ok')}  {_structured(wrote).get('output')}")
        lines.append("")

        listed = await session.call_tool("list_dir", {"path": "."})
        lines.append('client -> server: call list_dir {path: "."}')
        lines.append(f"server -> client: {_structured(listed).get('output')}")
        lines.append("")

        ran = await session.call_tool("run_command", {"command": "python hello.py"})
        sc = _structured(ran)
        lines.append('client -> server: call run_command {command: "python hello.py"}')
        lines.append(f"server -> client: ok={sc.get('ok')} exit={sc.get('exit_code')}  {str(sc.get('output', '')).strip()}")
        lines.append("")

        rejected = await session.call_tool("read_file", {"path": "/etc/passwd"})
        lines.append('client -> server: call read_file {path: "/etc/passwd"}   # absolute -> jailed')
        lines.append(f"server -> client: isError={rejected.isError}  {_first_text(rejected)}")

    lines.append("```")
    print("\n".join(lines))


if __name__ == "__main__":
    asyncio.run(main())
