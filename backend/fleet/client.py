"""A small client for the fleet api."""

import asyncio
import time
from collections.abc import Sequence

import httpx

from fleet.models import DEFAULT_MAX_ATTEMPTS, BatchStatus, NewJob, Submission
from fleet.policy import DEFAULT_POLICY, ExecutionPolicy


class FleetClient:
    def __init__(self, base_url: str = "", *, http: httpx.AsyncClient | None = None) -> None:
        self._http = http or httpx.AsyncClient(base_url=base_url, timeout=60)

    async def healthy(self) -> bool:
        try:
            response = await self._http.get("/health")
        except httpx.TransportError:
            return False
        return response.status_code == httpx.codes.OK

    async def submit(
        self,
        *,
        label: str,
        jobs: Sequence[NewJob],
        idempotency_key: str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        policy: ExecutionPolicy = DEFAULT_POLICY,
    ) -> Submission:
        response = await self._http.post(
            "/batches",
            json={
                "label": label,
                "idempotency_key": idempotency_key,
                "max_attempts": max_attempts,
                "policy": policy.model_dump(mode="json"),
                "jobs": [job.model_dump(mode="json") for job in jobs],
            },
        )
        response.raise_for_status()
        return Submission.model_validate(response.json())

    async def batch(self, batch_id: int) -> BatchStatus:
        response = await self._http.get(f"/batches/{batch_id}")
        response.raise_for_status()
        return BatchStatus.model_validate(response.json())

    async def wait_until_done(
        self, batch_id: int, *, poll_seconds: float = 0.2, timeout_seconds: float = 3600.0
    ) -> BatchStatus:
        deadline = time.monotonic() + timeout_seconds
        while not (status := await self.batch(batch_id)).done:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"batch {batch_id} unfinished after {timeout_seconds}s: {status}"
                )
            await asyncio.sleep(poll_seconds)
        return status

    async def aclose(self) -> None:
        await self._http.aclose()
