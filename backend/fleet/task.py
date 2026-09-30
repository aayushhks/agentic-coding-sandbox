"""Run one job inside its task container: the job comes in at /in, its result and progress go out
at /out. This process is trusted and the code the agent generates is not, though both run as the
same user, so it makes itself non-dumpable before anything else runs.
"""

import argparse
import asyncio
import ctypes
import json
import os
import sys
from pathlib import Path
from typing import Any

from fleet.progress import reporting_to
from fleet.runners import Runner, RunnerOutcome, load_runner

PR_SET_DUMPABLE = 4


def make_undumpable() -> None:
    """Make /proc/<pid> of this process root-owned, so same-user code can't read or trace it."""
    if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "could not make the task process non-dumpable")


def _write(path: Path, data: dict[str, Any]) -> None:
    # written whole, then renamed, so the worker never reads half a file
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(data))
    os.replace(partial, path)


async def _run(runner: Runner, job: dict[str, Any]) -> RunnerOutcome:
    return await runner(job["name"], job["payload"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", required=True, help="the job runner, as module:function")
    parser.add_argument("--input", type=Path, default=Path("/in/job.json"))
    parser.add_argument("--output", type=Path, default=Path("/out"))
    args = parser.parse_args(argv)
    make_undumpable()
    job = json.loads(args.input.read_text())
    runner = load_runner(args.runner)
    # line-buffered, so each event is on disk the moment it's reported
    with (args.output / "progress.jsonl").open("a", buffering=1) as progress:
        try:
            with reporting_to(lambda event: progress.write(json.dumps(event) + "\n")):
                outcome = asyncio.run(_run(runner, job))
        # the worker treats a runner that raised as an infrastructure failure
        except Exception as exc:
            _write(args.output / "error.json", {"error": f"{type(exc).__name__}: {exc}"})
            return 1
    _write(args.output / "result.json", outcome.model_dump(mode="json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
