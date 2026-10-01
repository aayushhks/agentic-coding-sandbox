"""When does each fleet worker come up, against when the fleet api answers its first health check?

    cd backend && PYTHONPATH=. .venv/bin/python ../docs/results/bench/m21/startup/measure.py

Starts the api and N workers the way the bench's fleet executor does, at N = 1, 2, 4, 8 and 16 in
turn, five times each with the order reversed every other round, and records how long after the
start the api answered its first health check (polled every 0.1 s, as the executor did) and how long
each worker took to write its ready file. Before the fix, the executor submitted the batch as soon
as the api answered.
"""

import asyncio
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

from bench.environment import capture_environment
from bench.fleet_executor import free_port, spawn
from fleet.client import FleetClient
from fleet.localdb import LocalPostgres
from fleet.migrate import migrate

POOLS = [1, 2, 4, 8, 16]
ROUNDS = 5
OUT = Path(__file__).resolve().parent / "startup.json"


async def start(url: str, workers: int) -> dict:
    port = free_port()
    env = {"FLEET_DATABASE_URL": url}
    with tempfile.TemporaryDirectory() as folder, open(os.devnull, "wb") as log:
        ready = [Path(folder) / f"w{index}.ready" for index in range(workers)]
        began = time.time()
        api = spawn(
            [
                *("-m", "uvicorn", "fleet.api:app", "--host", "127.0.0.1"),
                *("--port", str(port), "--log-level", "warning", "--no-access-log"),
            ],
            env,
            log,
        )
        pool = [
            spawn(
                [
                    *("-m", "fleet.worker", "--runner", "bench.runner:run_job"),
                    *("--worker-id", f"w{index}", "--database-url", url),
                    *("--ready-file", str(path)),
                ],
                env,
                log,
            )
            for index, path in enumerate(ready)
        ]
        client = FleetClient(f"http://127.0.0.1:{port}")
        try:
            while not await client.healthy():
                await asyncio.sleep(0.1)
            healthy = time.time() - began
            while not all(path.exists() for path in ready):
                await asyncio.sleep(0.01)
            up = sorted(path.stat().st_mtime - began for path in ready)
        finally:
            await client.aclose()
            for process in (*pool, api):
                process.send_signal(signal.SIGTERM)
            for process in (*pool, api):
                await asyncio.to_thread(process.wait, 30)
    return {
        "workers": workers,
        "api_healthy_seconds": round(healthy, 3),
        "workers_up_seconds": [round(seconds, 3) for seconds in up],
        "workers_up_after_api": sum(seconds > healthy for seconds in up),
    }


def main() -> None:
    environment = capture_environment().model_dump(mode="json")
    cluster = LocalPostgres.start()
    try:
        migrate(cluster.url)
        runs = []
        for round_ in range(1, ROUNDS + 1):
            for workers in POOLS if round_ % 2 else POOLS[::-1]:
                run = asyncio.run(start(cluster.url, workers)) | {"round": round_}
                print(json.dumps(run), flush=True)
                runs.append(run)
    finally:
        cluster.stop()
    OUT.write_text(json.dumps({"environment": environment, "runs": runs}, indent=2) + "\n")


if __name__ == "__main__":
    sys.exit(main())
