"""The fault scenarios: where each fault lands, what it does, and what it has to leave behind.

Every scenario injects its faults through failpoints armed in the processes it starts, except the
two that need nothing armed: a Postgres restart, which the harness does itself, and a ghost worker,
which claims jobs and never comes back. Each fault names the effects it must have on the record;
a fault that left none of them behind would mean the scenario tested nothing.
"""

import random
from dataclasses import dataclass

# what a fault must leave behind, checked against the database and the processes afterwards
EFFECTS = {
    "died": "the process the fault hit was killed",
    "survived": "the process the fault hit kept running to the end",
    "claim_undone": "the claim the fault interrupted left no attempt behind",
    "retried": "the attempt the fault hit ended with its lease lapsed, and a later one finished",
    "same_attempt": "the attempt the fault hit went on to publish the job's result",
    "stands": "the result the attempt published before the fault stands, and nothing ran again",
    "fenced": "the paused worker woke to find its lease gone, and stopped the run",
    "refused": "the paused worker woke and tried to publish, and its late result was refused",
    "counted": "the worker counted the result it had committed as published, not refused",
    "unblocked": "the job a hung worker held mid-transaction was finished by another attempt",
    "reaped_once": "each lapsed attempt the fault interrupted was ended once, by a later reap",
    "cancelled": "the job ended cancelled with no result, its attempt ended by the reaper",
    "one_batch": "the batch the client sent again exists once, and the client got it back",
    "all_survived": "every worker kept running to the end, through every restart",
}


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    point: str
    action: str
    summary: str
    effects: tuple[str, ...]
    faults: int = 3
    # sleep: keyed jobs of 50-600 ms; long: of 1.2-2 s; crash, cancel: some of them crash, or
    # run until cancelled; ghost: a worker claims some and vanishes; api: submitted through an
    # api that dies; agent: the bench's recorded tasks, replayed
    workload: str = "sleep"
    execution: str = "process"
    # the hits each armed process may fire on, so a fault lands on a different job each time
    hits: tuple[int, int] = (1, 3)
    # when a paused worker is woken: after a pause well past its lease, or only once a later
    # attempt holds its job, the moment a late write would do the most harm
    wake: str = "timer"
    # unarmed workers besides the armed ones, to take over the jobs of workers that are paused
    spares: int = 0

    def arms(self, rng: random.Random) -> list[str]:
        """The failpoint each worker started gets, in order, until the faults are spent."""
        if self.point in ("postgres.restart", "ghost.claim") or self.point.startswith("api."):
            return []
        action = "stop" if self.action == "hang" else self.action
        return [f"{self.point}={action}@{rng.randint(*self.hits)}" for _ in range(self.faults)]


def _kill(
    point: str,
    summary: str,
    *effects: str,
    workload: str = "sleep",
    hits: tuple[int, int] = (1, 3),
) -> Scenario:
    name = point.replace(".", "-").replace("_", "-") + "-kill"
    return Scenario(name, point, "kill", summary, ("died", *effects), workload=workload, hits=hits)


SCENARIOS = [
    _kill("claim.before", "a worker is killed as it is about to claim"),
    _kill(
        "claim.before_commit",
        "a worker is killed with its claim written but not committed",
        "claim_undone",
    ),
    Scenario(
        "claim-before-commit-drop",
        "claim.before_commit",
        "drop",
        "the connection drops with a claim written but not committed",
        ("survived",),
    ),
    Scenario(
        "claim-before-commit-hang",
        "claim.before_commit",
        "hang",
        "a worker hangs for good with its claim written but not committed",
        ("unblocked",),
        faults=1,
        hits=(1, 1),
    ),
    _kill("claim.after_commit", "a worker is killed just after its claim commits", "retried"),
    Scenario(
        "claim-after-commit-drop",
        "claim.after_commit",
        "drop",
        "a claim commits, but its answer never reaches the worker",
        ("survived", "retried"),
    ),
    _kill(
        "start.after_commit", "a worker is killed just after it marks the job running", "retried"
    ),
    _kill("heartbeat.before", "a worker is killed mid-task", "retried"),
    Scenario(
        "heartbeat-before-stop",
        "heartbeat.before",
        "stop",
        "a worker is paused mid-task until well past its lease",
        ("retried", "fenced"),
    ),
    Scenario(
        "heartbeat-before-drop",
        "heartbeat.before",
        "drop",
        "the connection drops mid-task, as the lease is extended",
        ("survived", "same_attempt"),
    ),
    _kill(
        "publish.before_commit",
        "a worker is killed with its result written but not committed",
        "retried",
    ),
    Scenario(
        "publish-before-stop",
        "publish.before",
        "stop",
        "a worker is paused well past its lease after its job ran, before it publishes",
        ("retried", "refused"),
        # woken while a later attempt is running the job, which its late result must not overwrite
        workload="long",
        wake="overtaken",
        spares=2,
    ),
    Scenario(
        "publish-before-commit-stop",
        "publish.before_commit",
        "stop",
        "a worker is paused well past its lease with its result written but not committed",
        # past a lease idle in its transaction, Postgres ends the session: the result never lands
        ("retried",),
    ),
    Scenario(
        "publish-before-commit-hang",
        "publish.before_commit",
        "hang",
        "a worker hangs for good with its result written but not committed",
        ("unblocked",),
        faults=1,
        hits=(1, 1),
    ),
    Scenario(
        "publish-before-commit-drop",
        "publish.before_commit",
        "drop",
        "the connection drops with a result written but not committed",
        ("survived", "same_attempt"),
    ),
    _kill("publish.after_commit", "a worker is killed just after its result commits", "stands"),
    Scenario(
        "publish-after-commit-drop",
        "publish.after_commit",
        "drop",
        "a result commits, but its answer never reaches the worker",
        ("survived", "stands", "counted"),
    ),
    _kill(
        "release.before_commit",
        "a worker is killed giving back a job whose runner crashed, before the commit",
        "retried",
        workload="crash",
    ),
    _kill(
        "reap.before_commit",
        "a worker is killed reaping lapsed leases, before the commit",
        "reaped_once",
        workload="ghost",
        hits=(1, 1),
    ),
    _kill(
        "cancel.before_commit",
        "a worker is killed ending a cancelled job, before the commit",
        "cancelled",
        workload="cancel",
        hits=(1, 1),
    ),
    _kill(
        "api.submit.after_commit",
        "the api is killed after a batch commits, before it answers",
        "one_batch",
        workload="api",
    ),
    Scenario(
        "postgres-restart",
        "postgres.restart",
        "restart",
        "Postgres stops the way a crash would and starts again, mid-batch",
        ("all_survived",),
    ),
    Scenario(
        "agent-heartbeat-before-kill",
        "heartbeat.before",
        "kill",
        "a worker is killed mid-task, running the real agent on recorded tasks",
        ("died", "retried"),
        workload="agent",
    ),
    Scenario(
        "agent-publish-before-commit-kill",
        "publish.before_commit",
        "kill",
        "a worker is killed with an agent's result written but not committed",
        ("died", "retried"),
        workload="agent",
    ),
    Scenario(
        "container-heartbeat-before-kill",
        "heartbeat.before",
        "kill",
        "a worker is killed mid-task while its job runs in a container of its own",
        ("died", "retried"),
        execution="container",
    ),
    Scenario(
        "container-publish-before-commit-kill",
        "publish.before_commit",
        "kill",
        "a worker is killed with a container's result written but not committed",
        ("died", "retried"),
        execution="container",
    ),
]
BY_NAME = {scenario.name: scenario for scenario in SCENARIOS}
