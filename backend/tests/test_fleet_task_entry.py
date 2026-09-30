"""The in-container entrypoint, run as a plain process: the container only adds the fences."""

import json
import subprocess
import sys
from pathlib import Path

from tests.fleet_helpers import BACKEND_ROOT


def _run_task(tmp_path: Path, payload: dict[str, object]) -> tuple[int, Path]:
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "in" / "job.json").write_text(json.dumps({"name": "j", "payload": payload}))
    done = subprocess.run(
        [
            *(sys.executable, "-m", "fleet.task", "--runner", "fleet.probes:run"),
            *("--input", str(tmp_path / "in" / "job.json"), "--output", str(tmp_path / "out")),
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return done.returncode, tmp_path / "out"


def test_a_task_writes_its_result_and_its_progress(tmp_path: Path) -> None:
    code, out = _run_task(tmp_path, {"probe": "sleep", "seconds": 0.3})
    assert code == 0
    result = json.loads((out / "result.json").read_text())
    assert (result["outcome"], result["body"]["steps"]) == ("succeeded", 3)
    progress = [json.loads(line) for line in (out / "progress.jsonl").read_text().splitlines()]
    assert progress == [{"step": 0}, {"step": 1}, {"step": 2}]


def test_a_runner_that_raises_leaves_an_error_and_no_result(tmp_path: Path) -> None:
    code, out = _run_task(tmp_path, {"probe": "raise"})
    assert code == 1
    assert not (out / "result.json").exists()
    error = json.loads((out / "error.json").read_text())
    assert error == {"error": "RuntimeError: the probe failed underneath the task"}


def test_the_task_process_makes_itself_non_dumpable() -> None:
    probe = (
        "import ctypes; from fleet.task import make_undumpable; "
        "make_undumpable(); print(ctypes.CDLL(None).prctl(3, 0, 0, 0, 0))"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], cwd=BACKEND_ROOT, capture_output=True, text=True
    )
    # PR_GET_DUMPABLE answers 0 once the process can't be read or traced by its own user
    assert done.stdout.strip() == "0", done.stderr
