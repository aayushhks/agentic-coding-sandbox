"""Failure-mode taxonomy and classification for unsolved tasks."""

from enum import StrEnum

from app.agent.types import TerminationReason
from app.benchmark.runner import TaskResult


class FailureMode(StrEnum):
    TIMED_OUT = "timed_out"
    EXHAUSTED_ITERATIONS = "exhausted_iterations"
    WRONG_SOLUTION = "wrong_solution"
    MALFORMED_TOOL_CALL = "malformed_tool_call"
    PROVIDER_ERROR = "provider_error"
    SANDBOX_ERROR = "sandbox_error"


def classify_failure(result: TaskResult) -> FailureMode | None:
    """Classify why a task was not solved, or return None if it was solved.

    Order matters: a provider crash or malformed-output loop is reported even though the
    hidden tests also fail, because that is the more informative root cause.
    """
    if result.solved:
        return None
    run = result.run
    if run.termination_reason == TerminationReason.PROVIDER_ERROR:
        return FailureMode.PROVIDER_ERROR
    if run.termination_reason == TerminationReason.MALFORMED_LIMIT:
        return FailureMode.MALFORMED_TOOL_CALL
    timed_out = result.evaluation.timed_out or any(
        step.tool_result is not None and step.tool_result.timed_out for step in run.steps
    )
    if timed_out:
        return FailureMode.TIMED_OUT
    if any("sandbox error" in step.observation for step in run.steps):
        return FailureMode.SANDBOX_ERROR
    if run.termination_reason == TerminationReason.MAX_ITERATIONS:
        return FailureMode.EXHAUSTED_ITERATIONS
    return FailureMode.WRONG_SOLUTION
