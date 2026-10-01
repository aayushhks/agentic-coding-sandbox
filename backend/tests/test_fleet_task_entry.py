"""The in-container entrypoint, run as a plain process: the container only adds the fences."""

import json
import os
import subprocess
import sys
from pathlib import Path

from fleet.execution import Reports, read_reports
from fleet.task import TOKEN_VARIABLE
from tests.fleet_helpers import BACKEND_ROOT

TOKEN = "5e3ad0c1f2b4a697"


def _run_task(
    tmp_path: Path, payload: dict[str, object], token: str | None = TOKEN
) -> tuple[int, str, str]:
    (tmp_path / "job.json").write_text(json.dumps({"name": "j", "payload": payload}))
    env = {key: value for key, value in os.environ.items() if key != TOKEN_VARIABLE}
    if token is not None:
        env[TOKEN_VARIABLE] = token
    done = subprocess.run(
        [
            *(sys.executable, "-m", "fleet.task", "--runner", "fleet.probes:run"),
            *("--input", str(tmp_path / "job.json")),
        ],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return done.returncode, done.stdout, done.stderr


def test_a_task_reports_its_progress_usage_and_result_on_its_stdout(tmp_path: Path) -> None:
    code, stdout, stderr = _run_task(tmp_path, {"probe": "sleep", "seconds": 0.3})
    assert code == 0, stderr
    reports = read_reports(stdout, TOKEN)
    assert reports.progress == [{"step": 0}, {"step": 1}, {"step": 2}]
    assert reports.result is not None
    assert (reports.result["outcome"], reports.result["body"]["steps"]) == ("succeeded", 3)
    assert reports.usage is not None and reports.usage["max_rss_mb"] > 0
    assert (reports.error, reports.printed) == (None, [])


def test_a_runner_that_raises_reports_an_error_and_no_result(tmp_path: Path) -> None:
    code, stdout, _ = _run_task(tmp_path, {"probe": "raise"})
    assert code == 1
    reports = read_reports(stdout, TOKEN)
    assert reports.result is None
    assert reports.error == "RuntimeError: the probe failed underneath the task"


def test_a_task_without_a_token_refuses_to_run(tmp_path: Path) -> None:
    code, stdout, stderr = _run_task(tmp_path, {"probe": "sleep", "seconds": 0.1}, token=None)
    assert (code, stdout) == (2, "")
    assert TOKEN_VARIABLE in stderr


def test_only_lines_carrying_the_attempts_token_are_reports() -> None:
    real = {"fleet": "result", "token": TOKEN, "result": {"outcome": "failed", "body": {}}}
    forged = {"fleet": "result", "token": "guessed", "result": {"outcome": "succeeded"}}
    untagged = {"fleet": "result", "result": {"outcome": "succeeded"}}
    lines = [json.dumps(forged), "plain output", json.dumps(real), json.dumps(untagged), "{"]
    reports = read_reports("\n".join(lines), TOKEN)
    assert reports.result == real["result"]
    # anything else, however it is dressed up, is only something that was printed
    assert reports.printed == [lines[0], lines[1], lines[3], lines[4]]
    assert read_reports("\n".join(lines[:2]), TOKEN) == Reports(printed=lines[:2])


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
