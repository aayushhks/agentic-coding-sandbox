"""Compare two ticket-eval reports.

A run regresses when a ticket the agent handled correctly before is now handled wrong — the
deployment-owner's version of the M7 benchmark regression diff.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.tickets.eval import TicketEvalReport


class Transition(StrEnum):
    IMPROVED = "improved"  # wrong in the baseline, correct in the candidate
    REGRESSED = "regressed"  # correct in the baseline, wrong in the candidate
    UNCHANGED_CORRECT = "unchanged_correct"
    UNCHANGED_WRONG = "unchanged_wrong"


@dataclass(frozen=True, slots=True)
class TicketRow:
    ticket_id: str
    category: str
    correct: bool
    outcome: str
    iterations: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class TicketDelta:
    ticket_id: str
    category: str
    transition: Transition
    baseline_correct: bool
    candidate_correct: bool
    baseline_outcome: str
    candidate_outcome: str
    iteration_delta: int
    token_delta: int


@dataclass(frozen=True, slots=True)
class TicketComparison:
    baseline_label: str
    candidate_label: str
    deltas: list[TicketDelta]

    @property
    def improved(self) -> list[TicketDelta]:
        return [d for d in self.deltas if d.transition == Transition.IMPROVED]

    @property
    def regressed(self) -> list[TicketDelta]:
        return [d for d in self.deltas if d.transition == Transition.REGRESSED]

    @property
    def has_regression(self) -> bool:
        return bool(self.regressed)

    @property
    def total_tickets(self) -> int:
        return len(self.deltas)

    @property
    def baseline_correct_count(self) -> int:
        return sum(1 for d in self.deltas if d.baseline_correct)

    @property
    def candidate_correct_count(self) -> int:
        return sum(1 for d in self.deltas if d.candidate_correct)

    @property
    def baseline_accuracy(self) -> float:
        return self.baseline_correct_count / self.total_tickets if self.deltas else 0.0

    @property
    def candidate_accuracy(self) -> float:
        return self.candidate_correct_count / self.total_tickets if self.deltas else 0.0

    @property
    def accuracy_delta(self) -> float:
        return self.candidate_accuracy - self.baseline_accuracy


def rows_from_report(report: TicketEvalReport) -> list[TicketRow]:
    return [
        TicketRow(
            ticket_id=o.ticket_id,
            category=o.category,
            correct=o.correct,
            outcome=o.outcome,
            iterations=o.iterations,
            total_tokens=o.total_tokens,
        )
        for o in report.outcomes
    ]


def _transition(baseline_correct: bool, candidate_correct: bool) -> Transition:
    if baseline_correct and candidate_correct:
        return Transition.UNCHANGED_CORRECT
    if not baseline_correct and not candidate_correct:
        return Transition.UNCHANGED_WRONG
    return Transition.IMPROVED if candidate_correct else Transition.REGRESSED


def compare_reports(
    baseline_label: str,
    candidate_label: str,
    baseline: Sequence[TicketRow],
    candidate: Sequence[TicketRow],
) -> TicketComparison:
    """Diff two runs ticket by ticket, rejecting runs that cover different ticket ids."""
    baseline_by_id = {row.ticket_id: row for row in baseline}
    candidate_by_id = {row.ticket_id: row for row in candidate}
    if baseline_by_id.keys() != candidate_by_id.keys():
        only_baseline = sorted(baseline_by_id.keys() - candidate_by_id.keys())
        only_candidate = sorted(candidate_by_id.keys() - baseline_by_id.keys())
        raise ValueError(
            f"reports cover different tickets: only in baseline={only_baseline}, "
            f"only in candidate={only_candidate}"
        )
    deltas: list[TicketDelta] = []
    for ticket_id in sorted(baseline_by_id):
        base = baseline_by_id[ticket_id]
        cand = candidate_by_id[ticket_id]
        deltas.append(
            TicketDelta(
                ticket_id=ticket_id,
                category=base.category,
                transition=_transition(base.correct, cand.correct),
                baseline_correct=base.correct,
                candidate_correct=cand.correct,
                baseline_outcome=base.outcome,
                candidate_outcome=cand.outcome,
                iteration_delta=cand.iterations - base.iterations,
                token_delta=cand.total_tokens - base.total_tokens,
            )
        )
    return TicketComparison(baseline_label, candidate_label, deltas)
