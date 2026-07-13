"""The MCP transport for the sandbox: a Sandbox backed by the sandbox MCP server.

`McpSandbox` spawns `app.mcp.sandbox_server` over stdio and drives it through a persistent MCP
`ClientSession`. Because it implements the same `Sandbox` interface the in-process sandbox does,
the agent runs unchanged whether its tools execute in-process or over MCP.

The session is async while `Sandbox.execute` is sync (the agent calls it in a worker thread, and
the benchmark harness calls it directly). Rather than a fragile per-call session, the session lives
on a dedicated event-loop thread for the sandbox's lifetime and each `execute` marshals a single
`call_tool` onto that loop with `run_coroutine_threadsafe`.
"""

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
import threading
from collections.abc import Coroutine, Sequence
from pathlib import Path
from typing import Any, TypeVar

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

from app.sandbox.base import Sandbox, SandboxConfig, SandboxError
from app.sandbox.tools import ToolCall, ToolName, ToolResult

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_T = TypeVar("_T")


class McpSandboxError(SandboxError):
    """Raised when the MCP sandbox transport cannot be established."""


def _first_text(blocks: Sequence[object]) -> str:
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    return ""


class McpSandbox(Sandbox):
    """A Sandbox that executes tool calls against the sandbox MCP server over stdio."""

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self._config = config or SandboxConfig()
        self._workspace = Path(tempfile.mkdtemp(prefix="acs-mcp-ws-")).resolve()
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._session: ClientSession | None = None
        self._err: BaseException | None = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._session is None:
            self.cleanup()
            raise McpSandboxError(f"failed to start the sandbox MCP server: {self._err}")

    @property
    def workspace(self) -> Path:
        return self._workspace

    def _server_params(self) -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "app.mcp.sandbox_server", "--workspace", str(self._workspace)],
            cwd=str(_BACKEND_ROOT),
            env=dict(os.environ),
        )

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as exc:  # recorded and surfaced to __init__ via self._err
            self._err = exc
        finally:
            self._ready.set()

    async def _serve(self) -> None:
        self._stop = asyncio.Event()
        async with (
            stdio_client(self._server_params()) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            self._session = session
            self._ready.set()
            await self._stop.wait()

    def _submit(self, coro: Coroutine[Any, Any, _T], timeout: float) -> _T:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

    def execute(self, call: ToolCall) -> ToolResult:
        if call.name in (ToolName.FINISH, ToolName.ESCALATE):
            key = "answer" if call.name == ToolName.FINISH else "reason"
            return ToolResult(output=str(call.arguments.get(key, "")), ok=True)
        if self._session is None:
            return ToolResult(output="sandbox error: MCP session is not available", ok=False)
        try:
            result = self._submit(
                self._session.call_tool(call.name.value, dict(call.arguments)),
                timeout=self._config.timeout_seconds + 30.0,
            )
        except Exception as exc:  # transport failures become failed observations, not crashes
            return ToolResult(output=f"sandbox error: {exc}", ok=False)
        return self._to_result(result)

    @staticmethod
    def _to_result(result: CallToolResult) -> ToolResult:
        structured = result.structuredContent
        if result.isError or structured is None:
            return ToolResult(output=_first_text(result.content) or "tool error", ok=False)
        return ToolResult(
            output=str(structured.get("output", "")),
            ok=bool(structured.get("ok", False)),
            exit_code=structured.get("exit_code"),
            timed_out=bool(structured.get("timed_out", False)),
            truncated=bool(structured.get("truncated", False)),
        )

    def cleanup(self) -> None:
        if self._stop is not None:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread.is_alive():
            self._thread.join(timeout=10.0)
        if not self._loop.is_closed():
            self._loop.close()
        shutil.rmtree(self._workspace, ignore_errors=True)
