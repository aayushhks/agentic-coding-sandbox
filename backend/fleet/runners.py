"""The runner contract: what a job runner returns, how it signals trouble, how it is named."""

import importlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel

from fleet.models import Outcome


class RunnerOutcome(BaseModel):
    outcome: Outcome
    body: dict[str, Any]


# (job name, payload) -> the task's final outcome; raising instead marks an infrastructure failure
Runner = Callable[[str, dict[str, Any]], Awaitable[RunnerOutcome]]


@dataclass(frozen=True, slots=True)
class RunnerError:
    """A runner raised: the failure was underneath the task, not in it."""

    error: str


def load_runner(path: str) -> Runner:
    """Import a runner named as module:function."""
    module_name, _, attribute = path.partition(":")
    if not module_name or not attribute:
        raise ValueError(f"a runner is named as module:function, got {path!r}")
    runner = getattr(importlib.import_module(module_name), attribute)
    if not callable(runner):
        raise TypeError(f"{path} is not callable")
    return cast(Runner, runner)
