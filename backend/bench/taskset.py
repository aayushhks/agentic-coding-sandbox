"""The stable bench task set and seeded job planning."""

import hashlib
import json
import random
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, model_validator

from app.benchmark.loader import load_benchmark
from app.benchmark.schema import Task
from app.tickets.loader import load_tickets
from app.tickets.models import ExpectedOutcome, TicketCase
from bench.probes import UNSATISFIABLE_SPEC

TASKSET_VERSION = "v1"
# the injection ticket stays out until tasks run inside the container sandbox
ESCALATION_TICKETS = ("TCK-04", "TCK-08")


class TaskKind(StrEnum):
    BENCHMARK = "benchmark"
    TICKET = "ticket"


class Expectation(StrEnum):
    SOLVE = "solve"
    ESCALATE = "escalate"
    FAIL = "fail"


class BenchTask(BaseModel):
    """One task in the set: what to run, and what a correct outcome looks like."""

    id: str
    kind: TaskKind
    category: str
    difficulty: str
    expected: Expectation
    benchmark: Task | None = None
    ticket: TicketCase | None = None

    @model_validator(mode="after")
    def _payload_matches_kind(self) -> Self:
        if self.kind == TaskKind.BENCHMARK and (self.benchmark is None or self.ticket is not None):
            raise ValueError(f"benchmark task {self.id!r} needs exactly a benchmark payload")
        if self.kind == TaskKind.TICKET and (self.ticket is None or self.benchmark is not None):
            raise ValueError(f"ticket task {self.id!r} needs exactly a ticket payload")
        return self


class TaskSet(BaseModel):
    version: str
    tasks: list[BenchTask]

    def digest(self) -> str:
        """A content hash of every task spec, so a record pins exactly what ran."""
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, task_id: str) -> BenchTask:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise KeyError(task_id)


def _from_benchmark(task: Task, expected: Expectation) -> BenchTask:
    return BenchTask(
        id=task.id,
        kind=TaskKind.BENCHMARK,
        category=task.category.value,
        difficulty=task.difficulty.value,
        expected=expected,
        benchmark=task,
    )


def _from_ticket(ticket: TicketCase) -> BenchTask:
    escalates = ticket.expected_outcome == ExpectedOutcome.ESCALATE
    return BenchTask(
        id=ticket.id,
        kind=TaskKind.TICKET,
        category=ticket.category,
        difficulty="n/a",
        expected=Expectation.ESCALATE if escalates else Expectation.SOLVE,
        ticket=ticket,
    )


def load_taskset() -> TaskSet:
    """The v1 set: the 15 benchmark tasks, two escalation tickets and one unsatisfiable probe."""
    tickets = {ticket.id: ticket for ticket in load_tickets()}
    tasks = [_from_benchmark(task, Expectation.SOLVE) for task in load_benchmark(version="v1")]
    tasks += [_from_ticket(tickets[ticket_id]) for ticket_id in ESCALATION_TICKETS]
    tasks.append(_from_benchmark(UNSATISFIABLE_SPEC, Expectation.FAIL))
    return TaskSet(version=TASKSET_VERSION, tasks=tasks)


@dataclass(frozen=True, slots=True)
class Job:
    """One unit of work in a batch: a task, and which repeat of it this is."""

    id: str
    task_id: str
    repeat: int


def plan_jobs(taskset: TaskSet, count: int, seed: int) -> list[Job]:
    """Seeded order: whole shuffled rounds of the set, with the last round sampled if partial."""
    if count < 1:
        raise ValueError("a batch needs at least one job")
    rng = random.Random(seed)
    ids = [task.id for task in taskset.tasks]
    jobs: list[Job] = []
    repeat = 0
    while len(jobs) < count:
        shuffled = rng.sample(ids, len(ids))
        jobs += [Job(f"{task_id}#{repeat}", task_id, repeat) for task_id in shuffled]
        repeat += 1
    return jobs[:count]
