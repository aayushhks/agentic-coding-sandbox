"""A subprocess-based sandbox with Linux-namespace network isolation.

This is the fallback the spec anticipates for environments without a usable Docker
daemon. It confines file operations to a temp workspace and runs every command under:
a wall-clock timeout, an output-size cap, rlimit-based CPU/memory/file-size caps, a
scrubbed environment, and (when available) a private network namespace via ``unshare``.

Honest boundary: this is process-level isolation, not a container. It does not virtualize
the filesystem or PID space, so it protects the host far less than Docker would. It is
appropriate for running the benchmark's own task code, not genuinely hostile programs.
"""

import contextlib
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import assert_never

from app.sandbox.base import Sandbox, SandboxArgumentError, SandboxConfig
from app.sandbox.tools import ToolCall, ToolName, ToolResult

_TRUNCATION_MARKER = "\n... [output truncated]"


def _build_limit_setter(config: SandboxConfig) -> Callable[[], None]:
    """Return a preexec callback that applies rlimits in the child before exec."""
    mem_bytes = config.memory_mb * 1024 * 1024
    fsize_bytes = config.max_file_size_mb * 1024 * 1024
    cpu_seconds = config.cpu_seconds

    def _apply_limits() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return _apply_limits


class SubprocessSandbox(Sandbox):
    def __init__(
        self, config: SandboxConfig | None = None, *, workspace: Path | None = None
    ) -> None:
        self._config = config or SandboxConfig()
        self._owns_workspace = workspace is None
        base = workspace or Path(tempfile.mkdtemp(prefix="agentic-sandbox-"))
        self._workspace = base.resolve()
        self._python = sys.executable
        self._env = self._build_env()
        self._limit_setter = _build_limit_setter(self._config)
        self._net_prefix = self._detect_net_prefix()
        self.network_isolated = self._config.network_disabled and bool(self._net_prefix)

    @property
    def workspace(self) -> Path:
        return self._workspace

    def cleanup(self) -> None:
        if self._owns_workspace:
            shutil.rmtree(self._workspace, ignore_errors=True)

    def execute(self, call: ToolCall) -> ToolResult:
        try:
            return self._dispatch(call)
        except SandboxArgumentError as exc:
            return ToolResult(output=f"error: {exc}", ok=False)

    def _dispatch(self, call: ToolCall) -> ToolResult:
        match call.name:
            case ToolName.READ_FILE:
                return self._read_file(self._require(call, "path"))
            case ToolName.WRITE_FILE:
                return self._write_file(self._require(call, "path"), self._require(call, "content"))
            case ToolName.LIST_DIR:
                return self._list_dir(str(call.arguments.get("path", ".")))
            case ToolName.RUN_COMMAND:
                return self._run_command(self._require(call, "command"))
            case ToolName.RUN_TESTS:
                target = call.arguments.get("target")
                return self._run_tests(str(target) if target is not None else None)
            case ToolName.FINISH:
                return ToolResult(output=str(call.arguments.get("answer", "")), ok=True)
        assert_never(call.name)

    @staticmethod
    def _require(call: ToolCall, key: str) -> str:
        if key not in call.arguments:
            raise SandboxArgumentError(f"{call.name} requires '{key}'")
        return str(call.arguments[key])

    def _resolve(self, rel_path: str) -> Path:
        candidate = (self._workspace / rel_path).resolve()
        if candidate != self._workspace and self._workspace not in candidate.parents:
            raise SandboxArgumentError(f"path '{rel_path}' escapes the workspace")
        return candidate

    def _read_file(self, path: str) -> ToolResult:
        try:
            resolved = self._resolve(path)
        except SandboxArgumentError as exc:
            return ToolResult(output=f"error: {exc}", ok=False)
        if not resolved.is_file():
            return ToolResult(output=f"error: no such file: {path}", ok=False)
        text = resolved.read_bytes().decode("utf-8", errors="replace")
        truncated = len(text.encode()) > self._config.max_output_bytes
        return ToolResult(
            output=self._truncate(text) if truncated else text, ok=True, truncated=truncated
        )

    def _write_file(self, path: str, content: str) -> ToolResult:
        try:
            resolved = self._resolve(path)
        except SandboxArgumentError as exc:
            return ToolResult(output=f"error: {exc}", ok=False)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return ToolResult(output=f"wrote {len(content)} bytes to {path}", ok=True)

    def _list_dir(self, path: str) -> ToolResult:
        try:
            resolved = self._resolve(path)
        except SandboxArgumentError as exc:
            return ToolResult(output=f"error: {exc}", ok=False)
        if not resolved.is_dir():
            return ToolResult(output=f"error: no such directory: {path}", ok=False)
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in resolved.iterdir())
        return ToolResult(output="\n".join(entries) if entries else "(empty)", ok=True)

    def _run_command(self, command: str) -> ToolResult:
        return self._as_result(self._run([*self._net_prefix, "/bin/sh", "-c", command]))

    def _run_tests(self, target: str | None) -> ToolResult:
        argv = [*self._net_prefix, self._python, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        argv.append(target if target else ".")
        return self._as_result(self._run(argv))

    @staticmethod
    def _as_result(run: tuple[int | None, str, bool, bool]) -> ToolResult:
        exit_code, output, timed_out, truncated = run
        return ToolResult(
            output=output or "(no output)",
            ok=(exit_code == 0 and not timed_out),
            exit_code=exit_code,
            timed_out=timed_out,
            truncated=truncated,
        )

    def _run(self, argv: list[str]) -> tuple[int | None, str, bool, bool]:
        proc = subprocess.Popen(
            argv,
            cwd=self._workspace,
            env=self._env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            preexec_fn=self._limit_setter,
            start_new_session=True,
        )
        timed_out = False
        try:
            output = proc.communicate(timeout=self._config.timeout_seconds)[0]
        except subprocess.TimeoutExpired:
            self._kill_group(proc)
            output = proc.communicate()[0] or ""
            timed_out = True
        truncated = len(output.encode()) > self._config.max_output_bytes
        if truncated:
            output = self._truncate(output)
        return proc.returncode, output, timed_out, truncated

    @staticmethod
    def _kill_group(proc: "subprocess.Popen[str]") -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)

    def _truncate(self, text: str) -> str:
        clipped = text.encode()[: self._config.max_output_bytes].decode("utf-8", errors="ignore")
        return clipped + _TRUNCATION_MARKER

    def _build_env(self) -> dict[str, str]:
        venv_bin = os.path.dirname(self._python)
        return {
            "PATH": f"{venv_bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self._workspace),
            "TMPDIR": str(self._workspace),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def _detect_net_prefix(self) -> list[str]:
        if not self._config.network_disabled:
            return []
        candidates: list[list[str]] = []
        if os.geteuid() == 0:
            candidates.append(["unshare", "--net", "--"])
        candidates.append(["unshare", "--user", "--map-root-user", "--net", "--"])
        for prefix in candidates:
            if self._probe(prefix):
                return prefix
        return []

    @staticmethod
    def _probe(prefix: list[str]) -> bool:
        try:
            result = subprocess.run([*prefix, "true"], capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0
