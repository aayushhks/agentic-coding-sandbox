"""Sandbox interface and configuration."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from app.sandbox.tools import ToolCall, ToolResult


class SandboxError(Exception):
    """Base class for sandbox errors."""


class SandboxArgumentError(SandboxError):
    """Raised when a tool call is missing a required argument or uses an invalid path."""


class SandboxUnavailableError(SandboxError):
    """Raised when isolation the configuration requires can't be set up on this host."""


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """Resource and isolation limits applied to every command the agent runs."""

    timeout_seconds: float = 10.0
    max_output_bytes: int = 10_000
    memory_mb: int = 4096
    cpu_seconds: int = 15
    max_file_size_mb: int = 64
    network_disabled: bool = True
    # refuse to run commands at all when the sandbox can't give them their own namespaces
    require_isolation: bool = False
    # directories commands must not see: each is covered by an empty read-only filesystem
    hidden_paths: tuple[str, ...] = ()


class Sandbox(ABC):
    """An isolated environment the agent acts on through tool calls.

    Implementations confine all file operations to a workspace directory and run commands
    under resource limits. A Docker-backed implementation can be added behind this same
    interface for environments with a usable daemon.
    """

    @property
    @abstractmethod
    def workspace(self) -> Path:
        """The root directory the agent operates in."""

    @abstractmethod
    def execute(self, call: ToolCall) -> ToolResult:
        """Execute a single tool call and return the observation."""

    @abstractmethod
    def cleanup(self) -> None:
        """Release any resources held by the sandbox."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.cleanup()
