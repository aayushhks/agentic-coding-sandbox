from collections import Counter

import pytest
from pydantic import ValidationError

from bench.probes import UNSATISFIABLE_SPEC
from bench.taskset import BenchTask, Expectation, TaskKind, load_taskset, plan_jobs


def test_taskset_covers_the_happy_and_unhappy_paths() -> None:
    taskset = load_taskset()
    ids = [task.id for task in taskset.tasks]
    assert len(ids) == 18
    assert len(set(ids)) == 18
    expected = Counter(task.expected for task in taskset.tasks)
    assert expected == {Expectation.SOLVE: 15, Expectation.ESCALATE: 2, Expectation.FAIL: 1}
    tickets = {task.id for task in taskset.tasks if task.kind == TaskKind.TICKET}
    assert tickets == {"TCK-04", "TCK-08"}
    assert taskset.get("unsatisfiable_spec").expected == Expectation.FAIL


def test_solvable_tasks_mix_difficulties_evenly() -> None:
    solvable = [task for task in load_taskset().tasks if task.expected == Expectation.SOLVE]
    assert Counter(task.difficulty for task in solvable) == {"easy": 5, "medium": 5, "hard": 5}


def test_digest_is_stable_and_changes_with_any_task_content() -> None:
    first, second = load_taskset(), load_taskset()
    assert first.digest() == second.digest()
    task = second.tasks[0]
    assert task.benchmark is not None
    task.benchmark.metadata.description += " (edited)"
    assert first.digest() != second.digest()


def test_plan_jobs_is_reproducible_for_a_seed() -> None:
    taskset = load_taskset()
    assert plan_jobs(taskset, 18, seed=1) == plan_jobs(taskset, 18, seed=1)
    assert plan_jobs(taskset, 18, seed=1) != plan_jobs(taskset, 18, seed=2)


def test_plan_jobs_repeats_whole_rounds_beyond_the_set_size() -> None:
    taskset = load_taskset()
    jobs = plan_jobs(taskset, 40, seed=3)
    assert len(jobs) == 40
    assert len({job.id for job in jobs}) == 40
    assert Counter(job.task_id for job in jobs[:36]) == {task.id: 2 for task in taskset.tasks}
    assert {job.repeat for job in jobs} == {0, 1, 2}


def test_plan_jobs_samples_distinct_tasks_for_a_partial_round() -> None:
    jobs = plan_jobs(load_taskset(), 5, seed=4)
    assert len({job.task_id for job in jobs}) == 5


def test_plan_jobs_rejects_an_empty_batch() -> None:
    with pytest.raises(ValueError, match="at least one job"):
        plan_jobs(load_taskset(), 0, seed=1)


def test_task_payload_must_match_its_kind() -> None:
    with pytest.raises(ValidationError, match="ticket payload"):
        BenchTask(
            id="x",
            kind=TaskKind.TICKET,
            category="bugfix",
            difficulty="easy",
            expected=Expectation.SOLVE,
            benchmark=UNSATISFIABLE_SPEC,
        )
