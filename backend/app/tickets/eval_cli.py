"""Command-line entrypoint: run the ticket eval, print the deployment report, optionally write JSON.

    python -m app.tickets.eval_cli --label groq-tickets --output docs/results/tickets.json

Uses the configured provider (LLM_PROVIDER / GROQ_API_KEY); the mock provider runs without a key
but cannot resolve or escalate, so a meaningful report needs a real model.
"""

import argparse
import asyncio
from pathlib import Path

from app.core.config import get_settings
from app.llm.base import LLMProvider
from app.llm.factory import build_provider
from app.tickets.eval import TicketEvalReport, run_ticket_eval, write_report_json
from app.tickets.loader import load_tickets


async def run_ticket_evaluation(
    *, label: str, provider: LLMProvider | None = None, output: Path | None = None
) -> TicketEvalReport:
    """Run the ticket eval with the configured (or supplied) provider and return the report."""
    settings = get_settings()
    active = provider or build_provider(settings)
    tickets = load_tickets()

    def factory(_ticket: object) -> LLMProvider:
        return active

    report = await run_ticket_eval(
        tickets, factory, label=label, provider_name=active.name, model=active.model
    )
    if output is not None:
        write_report_json(report, output)
    return report


def print_report(report: TicketEvalReport) -> None:
    stats = report.stats()
    tokens = stats["tokens_per_ticket"]
    latency = stats["latency_seconds_per_ticket"]
    print(f"ticket eval  label={report.label}  provider={report.provider}  model={report.model}")
    print(f"accuracy: {report.accuracy:.1%}  ({report.correct_count}/{report.total_tickets})")
    print(
        f"resolution rate: {report.resolution_rate:.1%}  "
        f"correct-escalation rate: {report.correct_escalation_rate:.1%}"
    )
    print(
        f"false-fix rate: {report.false_fix_rate:.1%}  "
        f"injection resistance: {report.injection_resistance:.1%}"
    )
    print(f"mean iterations: {report.mean_iterations:.1f}  total tokens: {report.total_tokens}")
    print(f"tokens/ticket p50/p95: {tokens['p50']:.0f} / {tokens['p95']:.0f}")
    print(f"latency/ticket p50/p95: {latency['p50']:.1f}s / {latency['p95']:.1f}s")
    print(f"failure taxonomy: {stats['failure_taxonomy']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the production-readiness ticket eval.")
    parser.add_argument("--label", default="ticket-run", help="label stored with this run")
    parser.add_argument("--output", type=Path, default=None, help="write the JSON report here")
    args = parser.parse_args()
    report = asyncio.run(run_ticket_evaluation(label=args.label, output=args.output))
    print_report(report)


if __name__ == "__main__":
    main()
