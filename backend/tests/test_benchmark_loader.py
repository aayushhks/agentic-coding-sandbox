import ast

from app.benchmark.loader import load_benchmark
from app.benchmark.schema import Task, TaskCategory, TaskDifficulty

BENCHMARK = load_benchmark()


def test_benchmark_has_enough_tasks() -> None:
    assert len(BENCHMARK) >= 15


def test_task_ids_are_unique() -> None:
    ids = [task.id for task in BENCHMARK]
    assert len(ids) == len(set(ids))


def test_every_task_has_hidden_tests_and_reference() -> None:
    for task in BENCHMARK:
        assert task.test_files, f"{task.id} has no hidden tests"
        assert task.reference_files, f"{task.id} has no reference solution"


def test_python_files_are_syntactically_valid() -> None:
    for task in BENCHMARK:
        for files in (task.workspace_files, task.test_files, task.reference_files):
            for path, content in files.items():
                if path.endswith(".py"):
                    ast.parse(content)


def test_tasks_use_valid_enums() -> None:
    for task in BENCHMARK:
        assert isinstance(task, Task)
        assert task.category in set(TaskCategory)
        assert task.difficulty in set(TaskDifficulty)


def test_benchmark_spans_categories_and_difficulties() -> None:
    categories = {task.category for task in BENCHMARK}
    difficulties = {task.difficulty for task in BENCHMARK}
    assert len(categories) >= 3
    assert difficulties == set(TaskDifficulty)
