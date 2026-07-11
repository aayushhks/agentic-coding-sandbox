"""A few starter tickets so the tracker server is demonstrable out of the box.

These are deliberately clean, well-specified tickets. The messy dataset FDE work is really about —
underspecified, conflicting, duplicate, escalate-me, and a prompt-injection ticket — arrives in
M12.
"""

from datetime import UTC, datetime
from pathlib import Path

from app.tracker.models import Ticket, TicketPriority, TicketStatus
from app.tracker.store import write_tickets

_SEED_TIME = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)

SEED_TICKETS: tuple[Ticket, ...] = (
    Ticket(
        id="ACS-101",
        title="CSV export drops the last row without a trailing newline",
        body=(
            "The CSV exporter omits the final record when the source file has no trailing "
            "newline. Fix the exporter so every row is written, and add a regression test."
        ),
        status=TicketStatus.OPEN,
        priority=TicketPriority.HIGH,
        workspace="reporting-service",
        labels=["bug", "exports"],
        created_at=_SEED_TIME,
        updated_at=_SEED_TIME,
    ),
    Ticket(
        id="ACS-102",
        title="Add a --version flag to the CLI",
        body=(
            "The CLI has no way to print its version. Add a --version flag that prints the package "
            "version and exits 0, and cover it with a test."
        ),
        status=TicketStatus.OPEN,
        priority=TicketPriority.MEDIUM,
        workspace="cli-tools",
        labels=["enhancement", "cli"],
        created_at=_SEED_TIME,
        updated_at=_SEED_TIME,
    ),
    Ticket(
        id="ACS-103",
        title="slugify() crashes on non-ASCII input",
        body=(
            "slugify() raises on strings with accented characters. It should transliterate or "
            "strip them and return a valid slug. Add tests for a few non-ASCII inputs."
        ),
        status=TicketStatus.OPEN,
        priority=TicketPriority.MEDIUM,
        workspace="web-utils",
        labels=["bug"],
        created_at=_SEED_TIME,
        updated_at=_SEED_TIME,
    ),
    Ticket(
        id="ACS-104",
        title="Retry HTTP calls on 429 with backoff",
        body=(
            "The API client gives up immediately on a 429. Add bounded exponential-backoff retries "
            "for 429 and 503 responses, capped at five attempts, and test the schedule."
        ),
        status=TicketStatus.IN_PROGRESS,
        priority=TicketPriority.LOW,
        workspace="api-client",
        labels=["reliability"],
        created_at=_SEED_TIME,
        updated_at=_SEED_TIME,
    ),
)


def write_seed(path: Path) -> None:
    """Write the starter tickets to a JSON file, overwriting any existing content."""
    write_tickets(path, list(SEED_TICKETS))
