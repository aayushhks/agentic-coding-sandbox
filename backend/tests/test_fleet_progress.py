import asyncio
from typing import Any

from fleet.progress import report, reporting_to


def test_progress_goes_nowhere_outside_a_reporting_context() -> None:
    report({"step": 0})


def test_progress_goes_to_the_innermost_sink_and_back() -> None:
    outer: list[dict[str, Any]] = []
    inner: list[dict[str, Any]] = []
    with reporting_to(outer.append):
        report({"n": 1})
        with reporting_to(inner.append):
            report({"n": 2})
        report({"n": 3})
    assert (outer, inner) == ([{"n": 1}, {"n": 3}], [{"n": 2}])


async def test_concurrent_jobs_report_to_their_own_sinks() -> None:
    async def job(tag: str, sink: list[dict[str, Any]]) -> None:
        with reporting_to(sink.append):
            for step in range(3):
                report({"job": tag, "step": step})
                await asyncio.sleep(0)

    first: list[dict[str, Any]] = []
    second: list[dict[str, Any]] = []
    await asyncio.gather(job("a", first), job("b", second))
    assert {event["job"] for event in first} == {"a"}
    assert {event["job"] for event in second} == {"b"}
    assert len(first) == len(second) == 3
