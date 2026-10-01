"""The M19 measurement commands, run small: they produce a record, and it says what it measured."""

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from bench import cancellation, container_overhead


async def test_the_overhead_command_times_the_same_probes_in_process_and_in_a_container(
    fleet_engine: AsyncEngine, fleet_database_url: str, task_image: str, tmp_path: Path
) -> None:
    out = tmp_path / "overhead.json"
    arguments = ["--rounds", "1", "--repeat", "1", "--out", str(out)]
    code = await asyncio.to_thread(
        container_overhead.main, [*arguments, "--database-url", fleet_database_url]
    )
    assert code == 0
    record = json.loads(out.read_text())
    assert record["config"]["image_id"].startswith("sha256:")
    for mode in ("process", "container"):
        summary = record["summary"][mode]
        assert summary["import_seconds"] > summary["python_startup_seconds"] > 0
        assert summary["sandbox_true_seconds"] > 0 and summary["sandbox_pytest_version_seconds"] > 0
    inside = record["summary"]["container"]
    # a container's own time is part of its job's service time, never more
    assert 0 < inside["nothing_container_seconds"] < inside["nothing_service_seconds"]
    assert "nothing_container_seconds" not in record["summary"]["process"]


async def test_the_cancel_command_measures_both_paths_and_checks_what_they_left(
    fleet_engine: AsyncEngine,
    fleet_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a short lease keeps each round's wait for a heartbeat under a second
    monkeypatch.setenv("FLEET_LEASE_SECONDS", "1.5")
    out = tmp_path / "cancel.json"
    arguments = ["--execution", "process", "--rounds", "2", "--out", str(out)]
    code = await asyncio.to_thread(
        cancellation.main, [*arguments, "--database-url", fleet_database_url]
    )
    record = json.loads(out.read_text())
    assert code == 0, record["checks"]
    assert (record["config"]["lease_seconds"], record["config"]["heartbeat_seconds"]) == (1.5, 0.5)
    assert record["checks"] == {
        "invariant_violations": [],
        "containers_left": 0,
        "running_jobs_cancelled_on_first_attempt": True,
        "queued_jobs_never_claimed": True,
    }
    for item in record["rounds"]:
        running = item["running"]
        # requested inside the first heartbeat interval, so noticed at its end
        assert 0 < running["requested_into_run_seconds"] < 0.5
        assert 0 < running["until_heartbeat_seconds"] <= running["latency_seconds"] < 1.5
