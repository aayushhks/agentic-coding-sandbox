import os
from collections.abc import Iterator

import pytest

from app.sandbox.base import SandboxConfig
from app.sandbox.subprocess_sandbox import SubprocessSandbox
from app.sandbox.tools import ToolCall, ToolName


@pytest.fixture
def sandbox() -> Iterator[SubprocessSandbox]:
    sb = SubprocessSandbox(SandboxConfig(timeout_seconds=5.0, max_output_bytes=2000))
    try:
        yield sb
    finally:
        sb.cleanup()


def test_write_then_read_file(sandbox: SubprocessSandbox) -> None:
    wrote = sandbox.execute(ToolCall(ToolName.WRITE_FILE, {"path": "foo.txt", "content": "hello"}))
    assert wrote.ok
    read = sandbox.execute(ToolCall(ToolName.READ_FILE, {"path": "foo.txt"}))
    assert read.ok
    assert read.output == "hello"


def test_read_missing_file(sandbox: SubprocessSandbox) -> None:
    read = sandbox.execute(ToolCall(ToolName.READ_FILE, {"path": "nope.txt"}))
    assert not read.ok
    assert "no such file" in read.output


def test_write_creates_subdirs_and_list_dir(sandbox: SubprocessSandbox) -> None:
    sandbox.execute(ToolCall(ToolName.WRITE_FILE, {"path": "pkg/mod.py", "content": "x = 1\n"}))
    listing = sandbox.execute(ToolCall(ToolName.LIST_DIR, {"path": "."}))
    assert "pkg/" in listing.output


def test_path_traversal_is_blocked(sandbox: SubprocessSandbox) -> None:
    read = sandbox.execute(ToolCall(ToolName.READ_FILE, {"path": "../../etc/passwd"}))
    assert not read.ok
    assert "escapes the workspace" in read.output


def test_missing_required_argument(sandbox: SubprocessSandbox) -> None:
    result = sandbox.execute(ToolCall(ToolName.WRITE_FILE, {"path": "a.txt"}))
    assert not result.ok
    assert "requires 'content'" in result.output


def test_run_command_success(sandbox: SubprocessSandbox) -> None:
    result = sandbox.execute(ToolCall(ToolName.RUN_COMMAND, {"command": "echo hello-sandbox"}))
    assert result.ok
    assert result.exit_code == 0
    assert "hello-sandbox" in result.output


def test_run_command_nonzero_exit(sandbox: SubprocessSandbox) -> None:
    result = sandbox.execute(ToolCall(ToolName.RUN_COMMAND, {"command": "exit 3"}))
    assert not result.ok
    assert result.exit_code == 3


def test_run_command_times_out() -> None:
    sb = SubprocessSandbox(SandboxConfig(timeout_seconds=1.0))
    try:
        result = sb.execute(ToolCall(ToolName.RUN_COMMAND, {"command": "sleep 10"}))
        assert result.timed_out
        assert not result.ok
    finally:
        sb.cleanup()


def test_run_command_caps_output() -> None:
    sb = SubprocessSandbox(SandboxConfig(max_output_bytes=200))
    try:
        command = "for i in $(seq 1 1000); do echo line$i; done"
        result = sb.execute(ToolCall(ToolName.RUN_COMMAND, {"command": command}))
        assert result.truncated
        assert "[output truncated]" in result.output
        assert len(result.output) < 300
    finally:
        sb.cleanup()


def test_environment_is_scrubbed() -> None:
    os.environ["SANDBOX_LEAK_CHECK"] = "leaked-secret"
    try:
        sb = SubprocessSandbox()
        try:
            result = sb.execute(
                ToolCall(ToolName.RUN_COMMAND, {"command": "echo ${SANDBOX_LEAK_CHECK:-absent}"})
            )
            assert "absent" in result.output
            assert "leaked-secret" not in result.output
        finally:
            sb.cleanup()
    finally:
        del os.environ["SANDBOX_LEAK_CHECK"]


def test_run_tests_passes_on_a_trivial_suite(sandbox: SubprocessSandbox) -> None:
    sandbox.execute(
        ToolCall(
            ToolName.WRITE_FILE,
            {"path": "test_demo.py", "content": "def test_ok():\n    assert 1 + 1 == 2\n"},
        )
    )
    result = sandbox.execute(ToolCall(ToolName.RUN_TESTS))
    assert result.ok
    assert result.exit_code == 0


def test_run_tests_reports_failure(sandbox: SubprocessSandbox) -> None:
    sandbox.execute(
        ToolCall(
            ToolName.WRITE_FILE,
            {"path": "test_bad.py", "content": "def test_bad():\n    assert False\n"},
        )
    )
    result = sandbox.execute(ToolCall(ToolName.RUN_TESTS))
    assert not result.ok
    assert result.exit_code != 0


def test_network_is_blocked_when_isolated(sandbox: SubprocessSandbox) -> None:
    if not sandbox.network_isolated:
        pytest.skip("network isolation unavailable in this environment")
    command = "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)\""
    result = sandbox.execute(ToolCall(ToolName.RUN_COMMAND, {"command": command}))
    assert not result.ok


def test_finish_returns_answer(sandbox: SubprocessSandbox) -> None:
    result = sandbox.execute(ToolCall(ToolName.FINISH, {"answer": "all done"}))
    assert result.ok
    assert result.output == "all done"
