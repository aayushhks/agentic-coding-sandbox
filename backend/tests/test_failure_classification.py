from app.agent.types import AgentRun, AgentStep, TerminationReason
from app.benchmark.runner import Evaluation, TaskResult
from app.eval.failure import FailureMode, classify_failure
from app.sandbox.tools import ToolCall, ToolName, ToolResult


def _run(reason: TerminationReason, steps: list[AgentStep] | None = None) -> AgentRun:
    return AgentRun(
        steps=steps or [],
        termination_reason=reason,
        final_answer="",
        prompt_tokens=0,
        completion_tokens=0,
    )


def _result(solved: bool, run: AgentRun, *, eval_timed_out: bool = False) -> TaskResult:
    return TaskResult(
        task_id="t",
        category="algorithms",
        difficulty="easy",
        solved=solved,
        run=run,
        evaluation=Evaluation(
            solved=solved, exit_code=0 if solved else 1, output="", timed_out=eval_timed_out
        ),
    )


def test_solved_has_no_failure() -> None:
    assert classify_failure(_result(True, _run(TerminationReason.FINISHED))) is None


def test_provider_error() -> None:
    result = _result(False, _run(TerminationReason.PROVIDER_ERROR))
    assert classify_failure(result) == FailureMode.PROVIDER_ERROR


def test_malformed_tool_call() -> None:
    result = _result(False, _run(TerminationReason.MALFORMED_LIMIT))
    assert classify_failure(result) == FailureMode.MALFORMED_TOOL_CALL


def test_timed_out_via_eval() -> None:
    result = _result(False, _run(TerminationReason.FINISHED), eval_timed_out=True)
    assert classify_failure(result) == FailureMode.TIMED_OUT


def test_exhausted_iterations() -> None:
    result = _result(False, _run(TerminationReason.MAX_ITERATIONS))
    assert classify_failure(result) == FailureMode.EXHAUSTED_ITERATIONS


def test_wrong_solution() -> None:
    result = _result(False, _run(TerminationReason.FINISHED))
    assert classify_failure(result) == FailureMode.WRONG_SOLUTION


def test_sandbox_error() -> None:
    step = AgentStep(
        index=0,
        thought="",
        raw_response="",
        tool_call=ToolCall(ToolName.RUN_COMMAND, {"command": "x"}),
        tool_result=ToolResult(output="sandbox error: boom", ok=False),
        observation="sandbox error: boom",
        malformed=False,
        prompt_tokens=0,
        completion_tokens=0,
    )
    result = _result(False, _run(TerminationReason.MAX_ITERATIONS, [step]))
    assert classify_failure(result) == FailureMode.SANDBOX_ERROR
