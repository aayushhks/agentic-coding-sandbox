"""The chaos workload: each job sleeps, then publishes the key it carries, gives up, or crashes."""

import asyncio
from typing import Any

from fleet.runners import RunnerOutcome


async def run(name: str, payload: dict[str, Any]) -> RunnerOutcome:
    await asyncio.sleep(int(payload["sleep_ms"]) / 1000)
    kind = payload.get("kind", "ok")
    if kind == "crash":
        raise RuntimeError("crashed underneath the task")
    outcome = "failed" if kind == "gives_up" else "succeeded"
    return RunnerOutcome(outcome=outcome, body={"result_key": payload["key"]})
