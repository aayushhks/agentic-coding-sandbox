from app.benchmark.runner import grade, setup_workspace
from app.sandbox.base import SandboxConfig
from app.sandbox.subprocess_sandbox import SubprocessSandbox
from bench.probes import UNSATISFIABLE_SPEC


def test_unsatisfiable_spec_fails_even_with_its_reference_solution() -> None:
    sandbox = SubprocessSandbox(SandboxConfig(timeout_seconds=15.0))
    try:
        setup_workspace(sandbox, UNSATISFIABLE_SPEC.reference_files)
        evaluation = grade(sandbox, UNSATISFIABLE_SPEC)
    finally:
        sandbox.cleanup()
    assert not evaluation.solved
    assert "1 failed, 1 passed" in evaluation.output
