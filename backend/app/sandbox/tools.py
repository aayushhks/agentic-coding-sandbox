"""The tool interface the agent uses to act on the sandbox."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ToolName(StrEnum):
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    LIST_DIR = "list_dir"
    RUN_COMMAND = "run_command"
    RUN_TESTS = "run_tests"
    FINISH = "finish"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A single action the agent wants to take."""

    name: ToolName
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The observation returned to the agent after executing a tool call."""

    output: str
    ok: bool
    exit_code: int | None = None
    timed_out: bool = False
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A description of a tool, used to build the agent's system prompt."""

    name: ToolName
    description: str
    parameters: dict[str, str]


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        ToolName.READ_FILE,
        "Read a UTF-8 text file from the workspace.",
        {"path": "file path relative to the workspace root"},
    ),
    ToolSpec(
        ToolName.WRITE_FILE,
        "Create or overwrite a UTF-8 text file in the workspace.",
        {
            "path": "file path relative to the workspace root",
            "content": "the full file contents to write",
        },
    ),
    ToolSpec(
        ToolName.LIST_DIR,
        "List the entries of a directory in the workspace.",
        {"path": "directory path relative to the workspace root (defaults to '.')"},
    ),
    ToolSpec(
        ToolName.RUN_COMMAND,
        "Run a shell command in the workspace and observe its combined output.",
        {"command": "the shell command to run"},
    ),
    ToolSpec(
        ToolName.RUN_TESTS,
        "Run the task's pytest suite in the workspace.",
        {"target": "optional pytest target (file or node id); defaults to the whole suite"},
    ),
    ToolSpec(
        ToolName.FINISH,
        "Declare the task complete and stop the loop.",
        {"answer": "optional summary of what was accomplished"},
    ),
)
