"""The chaos harness injects what it plans, and a run whose faults never happened can't pass."""

from dataclasses import replace
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from chaos.__main__ import summarize
from chaos.harness import run_scenario
from chaos.scenarios import BY_NAME


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
