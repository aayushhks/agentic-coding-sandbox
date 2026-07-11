# M11 — MCP Layer

The project through M10 runs the agent against a self-contained benchmark. M11 is the first step of
the forward-deployed extension: it exposes the agent's capabilities over the **Model Context
Protocol** and wraps an internal resource — an issue tracker — as a second MCP server, so the agent
can operate through a real tool-integration layer rather than only in-process.

## Two servers

Both are built with the official MCP Python SDK (FastMCP, `mcp==1.28.1`) and speak the protocol over
stdio, so any off-the-shelf MCP client (Claude Desktop, an SDK client) can connect.

- **Sandbox server** (`app/mcp/sandbox_server.py`) — exposes the existing tool surface
  (`read_file`, `write_file`, `list_dir`, `run_command`, `run_tests`). Each call is delegated to a
  `SubprocessSandbox`, so the isolation (private network namespace, rlimits, wall-clock timeout,
  output cap) is unchanged. Results are returned as structured output mirroring the in-process
  `ToolResult` field-for-field.
- **Issue-tracker server** (`app/mcp/tracker_server.py`) — a custom MCP wrapper around an internal
  resource: `list_tickets`, `get_ticket`, `update_ticket_status`, `add_comment`. It is backed by a
  `TicketStore` (`app/tracker/`), a JSON-file stand-in whose shape mirrors Jira / Linear / GitHub
  Issues. `TicketStore` is the single seam a real integration replaces.

## One interface, two transports

The agent already depended only on the `Sandbox` interface (`execute(ToolCall) -> ToolResult`), so
the MCP path is a drop-in `Sandbox` implementation, `McpSandbox` — the agent loop and protocol are
untouched. A `tool_transport` setting (`in_process` | `mcp`) selects the implementation through
`make_sandbox`; the `in_process` branch returns the same `SubprocessSandbox` as before, so
**benchmark behavior is byte-for-byte unchanged** (every M1–M10 test still passes).

### The sync/async bridge

The MCP client is async; `Sandbox.execute` is sync (the agent calls it in a worker thread, the
harness calls it directly). Rather than a fragile per-call session, `McpSandbox` runs a persistent
`ClientSession` on a dedicated event-loop thread for the sandbox's lifetime and marshals each
`call_tool` onto it with `run_coroutine_threadsafe`. That keeps the session — and the server's
workspace — alive across calls while presenting a plain synchronous interface.

## Trust boundary

Security is part of the milestone, not an afterthought:

- **Workspace jail.** File tools are confined to a `--workspace` root. Absolute paths are rejected
  at the MCP boundary; any path resolving outside the root is rejected by the sandbox
  (`... escapes the workspace`). A test writes to an absolute path outside the root and asserts the
  file never appears.
- **Command isolation.** `run_command` / `run_tests` execute only inside the `SubprocessSandbox`
  (network-namespace cutoff, rlimits, timeout, output cap). This is process-level isolation, not a
  container — the same honest boundary documented for the in-process sandbox.
- **Input validation.** Tool arguments are validated at the MCP boundary (FastMCP schema
  validation); a missing required argument is a tool error, not a crash.
- **Outputs are data.** Tool results — and, in the tracker, ticket text — are untrusted data, never
  instructions. The prompt-injection defense that leans on this lands in M12.

## Conformance

Each server has a test that spawns it with the **real MCP client over stdio**, completes the
`initialize` handshake, lists tools, and calls several — asserting on real results, not merely that
the process starts. The transport, the factory, the trust boundary, and the ticket store are each
covered too.

## Connecting an MCP client

The README section *"Connect this to Claude Desktop (or any MCP client)"* has the exact config
snippet. Pointing a general-purpose client at the sandbox server and watching it discover and call
the tools is the demo.

## Honest notes

- **The tracker is a local stand-in.** Tickets live in a JSON file and the "customer" is fictional;
  a real API is confined to `app/tracker/store.py`.
- **The MCP sandbox server uses default resource limits.** Custom `SandboxConfig` limits are not yet
  threaded through to the server — the benchmark uses defaults, so the two transports match.
- **The CI smoke run** exercises the eval harness on a single benchmark task through the MCP
  transport with the mock provider; it becomes a single-*ticket* run once the ticket-resolution
  harness lands in M13.
