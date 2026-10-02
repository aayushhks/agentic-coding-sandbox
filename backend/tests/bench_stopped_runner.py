"""A fleet runner for tests: bench real jobs, except one task that hangs past any deadline and one
whose every attempt crashes."""

import asyncio
from typing import Any

from fleet.progress import report
from fleet.runners import RunnerOutcome
from tests import bench_real_runner

HANGS = "TCK-04"
CRASHES = "unsatisfiable_spec"
# the one step the hanging task reports before it hangs
HUNG_STEP = {
    "step": 0,
    "tool": "read_file",
    "ok": True,
    "malformed": False,
    "prompt_tokens": 120,
    "completion_tokens": 30,
    "model_seconds": 0.4,
    "retry_wait_seconds": 2.5,
}


async def run_job(name: str, payload: dict[str, Any]) -> RunnerOutcome:
    task_id = payload["task"]["id"]
    if task_id == HANGS:
        report(HUNG_STEP)
        await asyncio.sleep(3600)
    if task_id == CRASHES:
        raise RuntimeError("the model api is unreachable from this worker")
    return await bench_real_runner.run_job(name, payload)
