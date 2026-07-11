"""The issue-tracker store.

`TicketStore` is the seam a real integration replaces: implement it against Jira / Linear /
GitHub Issues and everything above it — the MCP tracker server, the agent — is unchanged. The
`JsonTicketStore` shipped here is a JSON-file-backed stand-in, honest about being a local mock.
"""

import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from app.tracker.models import Ticket, TicketComment, TicketStatus


class TicketNotFoundError(LookupError):
    """Raised when a ticket id is not present in the store."""


def _now() -> datetime:
    return datetime.now(UTC)


def write_tickets(path: Path, tickets: Sequence[Ticket]) -> None:
    """Serialize tickets to a JSON file, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [ticket.model_dump(mode="json") for ticket in tickets]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class TicketStore(ABC):
    """Read/write access to tickets. A real backend (Jira / Linear / GitHub) implements this."""

    @abstractmethod
    def list_tickets(self, status: TicketStatus | None = None) -> list[Ticket]:
        """Return all tickets, optionally filtered by status, newest-created first."""

    @abstractmethod
    def get_ticket(self, ticket_id: str) -> Ticket:
        """Return one ticket by id, or raise TicketNotFoundError."""

    @abstractmethod
    def update_status(self, ticket_id: str, status: TicketStatus) -> Ticket:
        """Set a ticket's status and return the updated ticket."""

    @abstractmethod
    def add_comment(self, ticket_id: str, author: str, body: str) -> Ticket:
        """Append a comment to a ticket and return the updated ticket."""


class JsonTicketStore(TicketStore):
    """A TicketStore backed by a JSON file, load-modify-saved on each write (fine at this scale)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> list[Ticket]:
        if not self._path.is_file():
            return []
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        return [Ticket.model_validate(item) for item in raw]

    def _find(self, tickets: list[Ticket], ticket_id: str) -> int:
        for index, ticket in enumerate(tickets):
            if ticket.id == ticket_id:
                return index
        raise TicketNotFoundError(ticket_id)

    def list_tickets(self, status: TicketStatus | None = None) -> list[Ticket]:
        tickets = sorted(self._load(), key=lambda t: t.created_at, reverse=True)
        if status is not None:
            tickets = [ticket for ticket in tickets if ticket.status == status]
        return tickets

    def get_ticket(self, ticket_id: str) -> Ticket:
        tickets = self._load()
        return tickets[self._find(tickets, ticket_id)]

    def update_status(self, ticket_id: str, status: TicketStatus) -> Ticket:
        tickets = self._load()
        index = self._find(tickets, ticket_id)
        updated = tickets[index].model_copy(update={"status": status, "updated_at": _now()})
        tickets[index] = updated
        write_tickets(self._path, tickets)
        return updated

    def add_comment(self, ticket_id: str, author: str, body: str) -> Ticket:
        tickets = self._load()
        index = self._find(tickets, ticket_id)
        comment = TicketComment(author=author, body=body, created_at=_now())
        current = tickets[index]
        updated = current.model_copy(
            update={"comments": [*current.comments, comment], "updated_at": _now()}
        )
        tickets[index] = updated
        write_tickets(self._path, tickets)
        return updated
