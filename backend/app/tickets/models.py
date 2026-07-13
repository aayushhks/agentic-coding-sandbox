"""Models for the messy-ticket dataset used to characterize real-world resolution behavior."""

from enum import StrEnum

from pydantic import BaseModel, Field


class ExpectedOutcome(StrEnum):
    RESOLVE = "resolve"  # the agent should fix it and pass the hidden tests
    ESCALATE = "escalate"  # the agent should escalate rather than attempt or comply


class TicketCase(BaseModel):
    """One ticket the agent is asked to resolve, plus the ground truth for evaluating it."""

    id: str
    title: str
    body: str
    # messy-case label: clean | underspecified | missing_file | conflicting | duplicate |
    # needs_human | injection
    category: str
    expected_outcome: ExpectedOutcome
    # the prompt-injection ticket: its body embeds instructions the agent must refuse to follow
    adversarial: bool = False
    workspace_files: dict[str, str] = Field(default_factory=dict)
    # hidden tests defining a correct resolution (RESOLVE tickets only)
    test_files: dict[str, str] = Field(default_factory=dict)
    # files that must still exist after the run, used to prove an injection did no damage
    canary_files: list[str] = Field(default_factory=list)
