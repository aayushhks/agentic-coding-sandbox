"""A keyed submit is sent again until the api answers; anything else is sent once."""

import httpx
import pytest

from fleet.client import FleetClient
from fleet.models import NewJob

JOBS = [NewJob(name="j", payload={})]
CREATED = {"batch_id": 7, "job_ids": [1], "created": False}


def _client(answers: list[httpx.Response | None]) -> tuple[FleetClient, list[httpx.Request]]:
    """A client whose api gives these answers in turn; None is an api that isn't there."""
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        reply = answers[len(sent) - 1]
        if reply is None:
            raise httpx.ConnectError("connection refused", request=request)
        return reply

    transport = httpx.MockTransport(answer)
    return FleetClient(http=httpx.AsyncClient(transport=transport, base_url="http://fleet")), sent


async def test_a_keyed_submit_is_sent_again_until_the_api_answers() -> None:
    client, sent = _client([None, httpx.Response(503), httpx.Response(200, json=CREATED)])
    submission = await client.submit(label="b", jobs=JOBS, idempotency_key="k", retry_seconds=10)
    assert submission.batch_id == 7 and len(sent) == 3
    # the same batch under the same key each time, so the store makes it once
    assert len({request.content for request in sent}) == 1


async def test_a_submit_without_a_key_or_refused_outright_is_sent_once() -> None:
    client, sent = _client([None])
    with pytest.raises(httpx.ConnectError):
        await client.submit(label="b", jobs=JOBS, retry_seconds=10)
    assert len(sent) == 1
    client, sent = _client([httpx.Response(409, json={"detail": "key reused"})])
    with pytest.raises(httpx.HTTPStatusError):
        await client.submit(label="b", jobs=JOBS, idempotency_key="k", retry_seconds=10)
    assert len(sent) == 1
