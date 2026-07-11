"""Issue-tracker data models.

The tracker is a local stand-in for a real ticketing API (Jira / Linear / GitHub Issues). These
models mirror the shape those APIs expose, so a real integration only has to map its payloads onto
these types — nothing downstream (the MCP server, the agent) changes.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class TicketStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class TicketPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TicketComment(BaseModel):
    """A single comment on a ticket. The agent adds these to report progress or an escalation."""

    author: str
    body: str
    created_at: datetime


class Ticket(BaseModel):
    """A maintenance ticket the agent is asked to resolve against a workspace."""

    id: str
    title: str
    body: str
    status: TicketStatus = TicketStatus.OPEN
    priority: TicketPriority = TicketPriority.MEDIUM
    # the workspace / repo the ticket concerns; the sandbox server is jailed to this root
    workspace: str
    labels: list[str] = Field(default_factory=list)
    comments: list[TicketComment] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
