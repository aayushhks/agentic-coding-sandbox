# MCP session transcript

A real session: an MCP SDK client connected to `app.mcp.sandbox_server` over stdio, completed the
handshake, listed its tools, and called them — including the absolute path the workspace jail
rejects. This is the reproducible, screenshot-free demonstration linked from the README.

Regenerate with `uv run --project backend python scripts/mcp_session_demo.py > docs/mcp-session.md`.

```text
client -> server: initialize
server -> client: ready  (server='acs-sandbox', protocol=2025-11-25)

client -> server: list_tools
server -> client: list_dir, read_file, run_command, run_tests, write_file

client -> server: call write_file {path: "hello.py", content: "print(...)"}
server -> client: ok=True  wrote 32 bytes to hello.py

client -> server: call list_dir {path: "."}
server -> client: hello.py

client -> server: call run_command {command: "python hello.py"}
server -> client: ok=True exit=0  hello from the sandbox

client -> server: call read_file {path: "/etc/passwd"}   # absolute -> jailed
server -> client: isError=True  Error executing tool read_file: path must be workspace-relative, not absolute: '/etc/passwd'
```
