"""Telling a dropped database connection from a failed call, and riding through the drop."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from sqlalchemy.exc import DBAPIError

logger = logging.getLogger(__name__)
# postgres error codes for a connection that went away, and for a server shutting down or still
# starting: connection exceptions, admin shutdown, crash shutdown, cannot connect now
_LOST_STATES = ("08", "57P01", "57P02", "57P03")
FIRST_DELAY_SECONDS = 0.05
MAX_DELAY_SECONDS = 1.0


def connection_lost(exc: BaseException) -> bool:
    """Whether a call failed because its database connection went away, not because of the call."""
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    # sqlalchemy keeps the driver's error as orig, and the driver's own cause behind that
    pending: list[BaseException | None] = [exc]
    for _ in range(8):
        if not pending:
            break
        item = pending.pop()
        if item is None:
            continue
        # refused, reset or timed out on the way to the server
        if isinstance(item, (ConnectionError, TimeoutError)):
            return True
        state = getattr(item, "sqlstate", None)
        if isinstance(state, str) and state.startswith(_LOST_STATES):
            return True
        pending += [getattr(item, "orig", None), item.__cause__]
    return False


async def ride_through[T](
    call: Callable[[], Awaitable[T]],
    *,
    seconds: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """Make a database call, and make it again on a fresh connection while connections drop."""
    deadline = clock() + seconds
    delay = FIRST_DELAY_SECONDS
    while True:
        try:
            return await call()
        except Exception as exc:
            if not connection_lost(exc) or clock() + delay > deadline:
                raise
            logger.warning("database connection lost (%s); trying again", type(exc).__name__)
        await sleep(delay)
        delay = min(delay * 2, MAX_DELAY_SECONDS)
