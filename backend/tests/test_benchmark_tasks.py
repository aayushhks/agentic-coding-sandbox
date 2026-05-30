import pytest

from app.benchmark.loader import load_benchmark
from app.benchmark.runner import grade, setup_workspace
from app.benchmark.schema import Task
from app.sandbox.base import SandboxConfig
from app.sandbox.subprocess_sandbox import SubprocessSandbox

BENCHMARK = load_benchmark()


@pytest.mark.parametrize("task", BENCHMARK, ids=[task.id for task in BENCHMARK])
def test_reference_solution_passes_hidden_tests(task: Task) -> None:
    """Every task must be solvable: its reference solution passes its own hidden suite."""
    sandbox = SubprocessSandbox(SandboxConfig(timeout_seconds=15.0))
    try:
        setup_workspace(sandbox, task.reference_files)
        evaluation = grade(sandbox, task)
        assert evaluation.solved, f"{task.id} reference failed:\n{evaluation.output}"
    finally:
        sandbox.cleanup()
