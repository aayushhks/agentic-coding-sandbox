"""A fleet runner for tests: bench real jobs, answered by the scripted provider, not the model."""

from typing import Any

import bench.runner
from app.llm.mock_provider import MockProvider
from fleet.runners import RunnerOutcome
from tests.bench_helpers import SCRIPTS


async def run_job(name: str, payload: dict[str, Any]) -> RunnerOutcome:
    script = SCRIPTS[payload["task"]["id"]]
    # a worker runs one job at a time, so swapping the provider per job is safe here
    bench.runner.real_provider = lambda model: MockProvider(script, model=model)
    return await bench.runner.run_job(name, payload)
