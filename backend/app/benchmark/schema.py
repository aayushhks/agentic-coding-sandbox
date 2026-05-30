"""Schema for benchmark tasks."""

from enum import StrEnum

from pydantic import BaseModel, Field


class TaskCategory(StrEnum):
    ALGORITHMS = "algorithms"
    BUGFIX = "bugfix"
    REFACTOR = "refactor"
    DATA_STRUCTURES = "data_structures"
    STRING_MANIPULATION = "string_manipulation"


class TaskDifficulty(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class TaskMetadata(BaseModel):
    """The contents of a task's task.json file."""

    id: str
    title: str
    description: str
    category: TaskCategory
    difficulty: TaskDifficulty
    tags: list[str] = Field(default_factory=list)


class Task(BaseModel):
    """A fully loaded task: metadata plus the file trees that define it.

    ``workspace_files`` are placed in the sandbox before the agent starts. ``test_files`` are
    the hidden suite that grades success (never shown to the agent). ``reference_files`` are a
    known-good solution used only to validate that the task is solvable.
    """

    metadata: TaskMetadata
    workspace_files: dict[str, str] = Field(default_factory=dict)
    test_files: dict[str, str]
    reference_files: dict[str, str]

    @property
    def id(self) -> str:
        return self.metadata.id

    @property
    def description(self) -> str:
        return self.metadata.description

    @property
    def category(self) -> TaskCategory:
        return self.metadata.category

    @property
    def difficulty(self) -> TaskDifficulty:
        return self.metadata.difficulty
