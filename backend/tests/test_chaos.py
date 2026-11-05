"""The chaos harness injects what it plans, and a run whose faults never happened can't pass."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncEngine

from chaos.__main__ import summarize
from chaos.harness import Evidence, _retried, run_scenario
from chaos.scenarios import BY_NAME
from fleet.invariants import AttemptRow, JobRow


async def test_a_scenario_injects_its_faults_and_leaves_the_effects_it_must(
    fleet_engine: AsyncEngine, fleet_database_url: str, tmp_path: Path
) -> None:
    record = await run_scenario(
        BY_NAME["claim-after-commit-kill"],
        0,
        url=fleet_database_url,
        cluster=None,
        workdir=tmp_path,
    )
    assert record["passed"], record["violations"]
    assert record["faults_injected"] == record["config"]["faults_planned"] == 3
    # each fault was a kill, and each killed worker's job went to a later attempt
    assert {fault["action"] for fault in record["faults"]} == {"kill"}
    assert record["counts"]["jobs_run_more_than_once"] >= 1


async def test_a_run_whose_faults_never_fire_fails(
    fleet_engine: AsyncEngine, fleet_database_url: str, tmp_path: Path
) -> None:
    # armed for a hit no worker reaches, so the faults are planned but never happen
    unreachable = replace(BY_NAME["publish-before-commit-kill"], hits=(1000, 1000))
    record = await run_scenario(
        unreachable, 0, url=fleet_database_url, cluster=None, workdir=tmp_path
    )
    assert not record["passed"]
    assert record["violations"]["chaos"] == ["0 of the 3 planned faults happened"]


def test_the_table_counts_trials_faults_and_violations_per_scenario() -> None:
    def run(name: str, seed: int, faults: int, broken: list[str]) -> dict[str, object]:
        scenario = BY_NAME[name]
        return {
            "scenario": name,
            "seed": seed,
            "point": scenario.point,
            "action": scenario.action,
            "summary": scenario.summary,
            "faults_injected": faults,
            "violations": {"invariants": broken, "outcomes": [], "effects": [], "chaos": []},
            "passed": not broken,
        }

    table = summarize(
        [
            run("claim-before-kill", 0, 3, []),
            run("claim-before-kill", 1, 3, ["nothing_lost (job 4): left running"]),
            run("postgres-restart", 0, 3, []),
        ]
    )
    assert (table["trials"], table["faults_injected"], table["violations"]) == (3, 9, 1)
    assert table["scenarios"]["claim-before-kill"]["failed_seeds"] == [1]
    assert list(table["scenarios"]) == ["claim-before-kill", "postgres-restart"]


def _lapsed(attempt: int, job: JobRow) -> tuple[dict[str, Any], Evidence]:
    """A worker killed mid-attempt, whose lease then lapsed, and how its job ended."""
    now = datetime.now(UTC)
    lapsed = AttemptRow(
        job_id=job.id,
        attempt=attempt,
        worker_id="w1",
        claimed_at=now,
        lease_expires_at=now,
        ended_at=now,
        ended_by="lease_expired",
    )
    seen = SimpleNamespace(jobs={job.id: job}, attempts={(job.id, attempt): lapsed})
    return {"job": job.id, "attempt": attempt, "process": "w1"}, cast(Evidence, seen)


def test_a_kill_on_a_job_s_last_attempt_leaves_it_dead_lettered_there() -> None:
    # job 15 in CI: its runner crashed on attempts 1-4, and the kill landed on attempt 5 of 5
    spent = JobRow(id=15, state="dead_lettered", attempt=5, max_attempts=5, result_id=None)
    assert _retried(*_lapsed(5, spent)) is None


def test_a_kill_before_the_last_attempt_still_needs_a_later_one() -> None:
    stopped = JobRow(id=7, state="dead_lettered", attempt=2, max_attempts=5, result_id=None)
    assert _retried(*_lapsed(2, stopped)) == (
        "job 7 never finished on a later attempt (dead_lettered, 2)"
    )
    finished = JobRow(id=7, state="succeeded", attempt=3, max_attempts=5, result_id=1)
    assert _retried(*_lapsed(2, finished)) is None


def test_a_last_attempt_the_job_did_not_end_on_is_a_violation() -> None:
    running = JobRow(id=9, state="running", attempt=5, max_attempts=5, result_id=None)
    assert _retried(*_lapsed(5, running)) == (
        "job 9 spent its last attempt, yet ended running at 5"
    )


def test_the_last_attempt_is_the_job_s_own_budget_not_the_harness_default() -> None:
    three = JobRow(id=4, state="dead_lettered", attempt=3, max_attempts=3, result_id=None)
    assert _retried(*_lapsed(3, three)) is None
    # a cancel on the last attempt ends the job cancelled, which no retried fault leaves
    cancelled = JobRow(id=4, state="cancelled", attempt=3, max_attempts=3, result_id=None)
    assert _retried(*_lapsed(3, cancelled)) == (
        "job 4 spent its last attempt, yet ended cancelled at 3"
    )
