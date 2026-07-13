"""Load and validate the messy-ticket dataset."""

from app.tickets.dataset import TICKETS
from app.tickets.models import ExpectedOutcome, TicketCase


def load_tickets() -> list[TicketCase]:
    """Return the ticket dataset, checking ids are unique and resolve tickets carry hidden tests."""
    tickets = list(TICKETS)
    ids = [ticket.id for ticket in tickets]
    duplicates = sorted({tid for tid in ids if ids.count(tid) > 1})
    if duplicates:
        raise ValueError(f"duplicate ticket ids: {duplicates}")
    for ticket in tickets:
        if ticket.expected_outcome == ExpectedOutcome.RESOLVE and not ticket.test_files:
            raise ValueError(f"resolve ticket {ticket.id!r} has no hidden tests")
    return tickets
