import json

from bench.report import REPORT_PATH, build_report, render


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
