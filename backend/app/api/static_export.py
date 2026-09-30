"""Write the dashboard API's answers as static JSON, so the dashboard can run with no backend."""

import argparse
import asyncio
import json
import re
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from app.api.runs import compare, get_run, get_task, list_runs
from app.db.session import create_engine, create_session_factory
from app.eval.import_results import import_results

_REPO_ROOT = Path(__file__).resolve().parents[3]
# the same committed runs the docker image bakes in
DEFAULT_RESULTS = (
    _REPO_ROOT / "docs" / "results" / "groq-llama-3.3-70b-v1.json",
    _REPO_ROOT / "docs" / "results" / "groq-llama-3.3-70b-v2.json",
)
DEFAULT_OUT = _REPO_ROOT / "frontend" / "public" / "static-api"
# ids and labels become file names, so they have to map one-to-one onto url path segments
_SAFE_NAME = re.compile(r"[A-Za-z0-9._-]+")


def safe_name(name: str) -> str:
    if not _SAFE_NAME.fullmatch(name):
        raise ValueError(f"{name!r} can't be used as a static file name")
    return name


async def export_static_api(
    out_dir: Path, results: Sequence[Path] = DEFAULT_RESULTS
) -> list[tuple[str, str]]:
    """Write every dashboard read as a file; returns (file, the api path it stands for) pairs."""
    written: list[tuple[str, str]] = []

    def emit(file: str, api_path: str, payload: Any) -> None:
        path = out_dir / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append((file, api_path))

    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite+aiosqlite:///{Path(tmp) / 'export.db'}"
        for index, path in enumerate(results):
            await import_results(path, database_url=url, create_tables=index == 0)
        engine = create_engine(url)
        try:
            async with create_session_factory(engine)() as session:
                runs = await list_runs(session)
                emit("runs.json", "/runs", [run.model_dump(mode="json") for run in runs])
                labels: list[str] = []
                for run in runs:
                    detail = await get_run(run.id, session)
                    emit(f"runs/{run.id}.json", f"/runs/{run.id}", detail.model_dump(mode="json"))
                    for result in detail.results:
                        task_id = safe_name(result.task_id)
                        task = await get_task(run.id, task_id, session)
                        emit(
                            f"runs/{run.id}/tasks/{task_id}.json",
                            f"/runs/{run.id}/tasks/{task_id}",
                            task.model_dump(mode="json"),
                        )
                    if run.label not in labels:
                        labels.append(safe_name(run.label))
                for baseline in labels:
                    for candidate in labels:
                        diff = await compare(session, baseline, candidate)
                        emit(
                            f"compare/{baseline}__{candidate}.json",
                            f"/compare?{urlencode({'baseline': baseline, 'candidate': candidate})}",
                            diff.model_dump(mode="json"),
                        )
        finally:
            await engine.dispose()
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="a static-api directory")
    args = parser.parse_args()
    out: Path = args.out
    if out.name != "static-api":
        parser.error("the output directory must be named static-api, since it is replaced")
    # replaced wholesale so a run or task that no longer exists doesn't leave a stale file
    shutil.rmtree(out, ignore_errors=True)
    written = asyncio.run(export_static_api(out))
    print(f"wrote {len(written)} files to {out}")


if __name__ == "__main__":
    main()
