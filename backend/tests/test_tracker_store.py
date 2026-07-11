"""Tests for the JSON ticket store — the local stand-in for a real ticketing API."""

from pathlib import Path

import pytest

from app.tracker.models import TicketStatus
from app.tracker.seed import SEED_TICKETS, write_seed
from app.tracker.store import JsonTicketStore, TicketNotFoundError


def _seeded_store(tmp_path: Path) -> JsonTicketStore:
    path = tmp_path / "tickets.json"
    write_seed(path)
    return JsonTicketStore(path)


def test_lists_all_seeded_tickets(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    assert len(store.list_tickets()) == len(SEED_TICKETS)


def test_filters_by_status(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    open_ids = {ticket.id for ticket in store.list_tickets(TicketStatus.OPEN)}
    assert "ACS-101" in open_ids


def test_get_missing_ticket_raises(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    with pytest.raises(TicketNotFoundError):
        store.get_ticket("NOPE")


def test_update_status_persists(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    store.update_status("ACS-101", TicketStatus.RESOLVED)
    assert store.get_ticket("ACS-101").status == TicketStatus.RESOLVED


def test_add_comment_persists(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    store.add_comment("ACS-101", "agent", "on it")
    ticket = store.get_ticket("ACS-101")
    assert len(ticket.comments) == 1
    assert ticket.comments[0].body == "on it"
