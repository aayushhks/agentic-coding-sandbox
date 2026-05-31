"""Generate matplotlib comparison figures for a v1-vs-v2 benchmark diff."""

from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

from app.eval.compare import RunComparison, Transition

_TRANSITION_COLORS: dict[Transition, str] = {
    Transition.CONVERTED: "#2ca02c",
    Transition.REGRESSED: "#d62728",
    Transition.UNCHANGED_PASS: "#7f7f7f",
    Transition.UNCHANGED_FAIL: "#c7c7c7",
}


def _solve_rate_by_category(
    comparison: RunComparison,
) -> tuple[list[str], list[float], list[float]]:
    categories = sorted({d.category for d in comparison.deltas})
    baseline_rates: list[float] = []
    candidate_rates: list[float] = []
    for category in categories:
        rows = [d for d in comparison.deltas if d.category == category]
        baseline_rates.append(sum(1 for d in rows if d.baseline_solved) / len(rows))
        candidate_rates.append(sum(1 for d in rows if d.candidate_solved) / len(rows))
    categories.append("overall")
    baseline_rates.append(comparison.baseline_solve_rate)
    candidate_rates.append(comparison.candidate_solve_rate)
    return categories, baseline_rates, candidate_rates


def solve_rate_by_category_figure(comparison: RunComparison) -> Figure:
    """Grouped bars of solve rate per category (plus an overall group), baseline vs candidate."""
    categories, baseline_rates, candidate_rates = _solve_rate_by_category(comparison)
    positions = range(len(categories))
    width = 0.38
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.bar(
        [p - width / 2 for p in positions],
        [r * 100 for r in baseline_rates],
        width,
        label=comparison.baseline_label,
        color="#1f77b4",
    )
    ax.bar(
        [p + width / 2 for p in positions],
        [r * 100 for r in candidate_rates],
        width,
        label=comparison.candidate_label,
        color="#ff7f0e",
    )
    ax.set_ylabel("solve rate (%)")
    ax.set_ylim(0, 105)
    ax.set_title("solve rate by category: baseline vs candidate")
    ax.set_xticks(list(positions))
    ax.set_xticklabels(categories, rotation=20, ha="right")
    ax.legend()
    fig.tight_layout()
    return fig


def per_task_token_delta_figure(comparison: RunComparison) -> Figure:
    """Horizontal bars of per-task token delta (candidate - baseline), colored by transition."""
    deltas = sorted(comparison.deltas, key=lambda d: d.token_delta)
    task_ids = [d.task_id for d in deltas]
    values = [d.token_delta for d in deltas]
    colors = [_TRANSITION_COLORS[d.transition] for d in deltas]
    fig, ax = plt.subplots(figsize=(9, 6))
    ax.barh(task_ids, values, color=colors)
    ax.axvline(0, color="black", linewidth=0.8)
    ax.set_xlabel("token delta (candidate - baseline)")
    ax.set_title("per-task token cost change (color = transition)")
    handles = [Rectangle((0, 0), 1, 1, color=color) for color in _TRANSITION_COLORS.values()]
    ax.legend(handles, [t.value for t in _TRANSITION_COLORS], loc="lower right", fontsize=8)
    fig.tight_layout()
    return fig


def write_comparison_figures(comparison: RunComparison, out_dir: Path) -> list[Path]:
    """Render the comparison figures into out_dir (headless) and return the written paths."""
    plt.switch_backend("Agg")
    out_dir.mkdir(parents=True, exist_ok=True)
    figures: dict[str, Figure] = {
        "solve_rate_by_category.png": solve_rate_by_category_figure(comparison),
        "per_task_token_delta.png": per_task_token_delta_figure(comparison),
    }
    written: list[Path] = []
    for name, fig in figures.items():
        path = out_dir / name
        fig.savefig(path, dpi=120)
        plt.close(fig)
        written.append(path)
    return written
