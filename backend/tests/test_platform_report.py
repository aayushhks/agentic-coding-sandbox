import json
from collections import Counter
from pathlib import Path

import pytest

import bench.report as report
from bench.report import REPORT_PATH, RESULTS, _one, build_report, render


def test_the_published_report_is_what_the_committed_records_say() -> None:
    # the page renders this file and nothing else; regenerate it with `python -m bench.report`
    committed = REPORT_PATH.read_text(encoding="utf-8")
    assert committed == render(build_report()), (
        "frontend/public/platform-report.json no longer matches the records under docs/results; "
        "run `python -m bench.report` and commit the result"
    )


def test_every_number_on_the_page_says_where_it_came_from() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    for item in [*report["headline"], *report["sections"]]:
        assert item["config"], item["id"]
        assert item["sources"] and item["doc"], item["id"]
        for source in [*item["sources"], item["doc"]]:
            assert (REPORT_PATH.parents[2] / source).exists(), source


def test_a_configuration_is_stated_only_where_every_record_agrees() -> None:
    assert _one([4, 4, 4], "workers") == 4
    with pytest.raises(ValueError, match="disagree on workers"):
        _one([4, 8], "workers")


def test_the_fault_matrix_states_every_shape_of_run_it_made() -> None:
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((RESULTS / "chaos" / "m20").glob("*/seed-*.json"))
    ]
    shapes = Counter((r["config"]["jobs"], r["config"]["workers"]) for r in records)
    [faults] = [item for item in build_report()["headline"] if item["id"] == "faults"]
    for (jobs, workers), count in shapes.items():
        assert f"{count} runs of {jobs} jobs on {workers} worker processes" in faults["config"]


def test_the_real_fleet_run_gets_a_section_once_its_record_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(report, "REAL_FLEET", tmp_path / "not-run-yet")
    assert report._real_fleet() is None
    # a sequential record in a fleet run's place has every field the section reads
    trial = json.loads(
        (RESULTS / "bench" / "m22" / "baseline" / "trial-1.json").read_text(encoding="utf-8")
    )
    trial["config"]["workers"] = 4
    trial["config"]["execution"] = {"mode": "process", "policy": {"timeout_seconds": 600.0}}
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "trial-1.json").write_text(json.dumps(trial), encoding="utf-8")
    monkeypatch.setattr(report, "REAL_FLEET", tmp_path / "run")
    monkeypatch.setattr(report, "_relative", str)
    section = report._real_fleet()
    assert section is not None
    batches, jobs = section["tables"]
    assert batches["rows"][0][:2] == ["4 fleet workers", "18 of 18"]
    assert [row[0] for row in batches["rows"][1:]] == [
        "M16, one worker in the bench's process",
        "M22, one worker in the bench's process",
    ]
    assert len(jobs["rows"]) == 18
    assert "600 s per job" in section["config"]
