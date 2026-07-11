"""Select which Sandbox implementation the agent's tools run through.

Both transports satisfy the same Sandbox interface, so the agent is identical either way:

- ``in_process``: the SubprocessSandbox used since M2 — the default, byte-for-byte unchanged.
- ``mcp``: the McpSandbox, which drives the sandbox MCP server over stdio.
"""

from app.mcp.sandbox_client import McpSandbox
from app.sandbox.base import Sandbox, SandboxConfig
from app.sandbox.subprocess_sandbox import SubprocessSandbox

IN_PROCESS = "in_process"
MCP = "mcp"


def make_sandbox(transport: str, config: SandboxConfig | None = None) -> Sandbox:
    """Build the Sandbox for the given transport, raising ValueError for an unknown one."""
    if transport == IN_PROCESS:
        return SubprocessSandbox(config)
    if transport == MCP:
        return McpSandbox(config)
    raise ValueError(f"unknown tool transport: {transport!r} (expected {IN_PROCESS!r} or {MCP!r})")
