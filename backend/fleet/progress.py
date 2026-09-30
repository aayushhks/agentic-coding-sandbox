"""Where a runner reports progress while its job runs, so a job cut short keeps what it did."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

Sink = Callable[[dict[str, Any]], None]

_sink: ContextVar[Sink | None] = ContextVar("fleet_progress_sink", default=None)


def report(event: dict[str, Any]) -> None:
    """Record one progress event; outside a reporting context it goes nowhere."""
    sink = _sink.get()
    if sink is not None:
        sink(event)


@contextmanager
def reporting_to(sink: Sink) -> Iterator[None]:
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)
