"""The sandbox factory maps the transport setting onto a Sandbox implementation."""

import pytest

from app.mcp.sandbox_client import McpSandbox
from app.sandbox.factory import make_sandbox
from app.sandbox.subprocess_sandbox import SubprocessSandbox


def test_in_process_returns_the_subprocess_sandbox() -> None:
    sandbox = make_sandbox("in_process")
    try:
        assert isinstance(sandbox, SubprocessSandbox)
    finally:
        sandbox.cleanup()


def test_mcp_returns_the_mcp_sandbox() -> None:
    sandbox = make_sandbox("mcp")
    try:
        assert isinstance(sandbox, McpSandbox)
    finally:
        sandbox.cleanup()


def test_unknown_transport_raises() -> None:
    with pytest.raises(ValueError, match="unknown tool transport"):
        make_sandbox("carrier-pigeon")
