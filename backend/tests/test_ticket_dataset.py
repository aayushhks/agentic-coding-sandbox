"""The messy-ticket dataset is well-formed and has the intended coverage."""

from collections import Counter

from app.tickets.loader import load_tickets
from app.tickets.models import ExpectedOutcome


def test_dataset_loads_with_unique_ids() -> None:
    tickets = load_tickets()
    assert len(tickets) == 10
    assert len({ticket.id for ticket in tickets}) == 10


def test_dataset_has_expected_outcome_mix() -> None:
    outcomes = Counter(ticket.expected_outcome for ticket in load_tickets())
    assert outcomes[ExpectedOutcome.RESOLVE] == 3
    assert outcomes[ExpectedOutcome.ESCALATE] == 7


def test_covers_the_messy_categories() -> None:
    categories = {ticket.category for ticket in load_tickets()}
    assert {
        "underspecified",
        "missing_file",
        "conflicting",
        "duplicate",
        "needs_human",
        "injection",
    } <= categories


def test_exactly_one_adversarial_ticket_with_canaries() -> None:
    adversarial = [ticket for ticket in load_tickets() if ticket.adversarial]
    assert len(adversarial) == 1
    assert adversarial[0].canary_files


def test_resolve_tickets_carry_hidden_tests() -> None:
    for ticket in load_tickets():
        if ticket.expected_outcome == ExpectedOutcome.RESOLVE:
            assert ticket.test_files, f"{ticket.id} has no hidden tests"
