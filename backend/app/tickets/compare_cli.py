"""Diff two ticket-eval report JSONs and exit non-zero on any regression.

python -m app.tickets.compare_cli --baseline base.json --candidate cand.json
"""

import argparse
import sys
from pathlib import Path

from app.tickets.compare import compare_reports, rows_from_report
from app.tickets.eval import load_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Diff two ticket-eval reports.")
    parser.add_argument("--baseline", type=Path, required=True, help="baseline report JSON")
    parser.add_argument("--candidate", type=Path, required=True, help="candidate report JSON")
    args = parser.parse_args()

    baseline = load_report(args.baseline)
    candidate = load_report(args.candidate)
    comparison = compare_reports(
        baseline.label, candidate.label, rows_from_report(baseline), rows_from_report(candidate)
    )

    print(
        f"accuracy: {comparison.baseline_accuracy:.1%} -> {comparison.candidate_accuracy:.1%}  "
        f"({comparison.accuracy_delta:+.1%})"
    )
    print(f"improved:  {[d.ticket_id for d in comparison.improved]}")
    print(f"regressed: {[d.ticket_id for d in comparison.regressed]}")
    if comparison.has_regression:
        print("REGRESSION DETECTED")
        sys.exit(1)
    print("no regressions")


if __name__ == "__main__":
    main()
