"""Recompute every M22 number from the committed records.

    python3 docs/results/bench/m22/analysis.py

Standard library only. It prints what each arm ran with (the agent digest, the model and builds that
answered, the price), each trial of each arm, every task's outcomes and tokens in each arm, the first
round's tokens per task beside m16's real run of the unchanged prompt, the comparison the bench
generated, and whether replaying each arm's first trial from its own recorded responses reproduced
it job for job.
"""

import json
import statistics
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARMS = ("baseline", "short-thoughts")

median = statistics.median


def _trials(folder: Path) -> list[dict]:
    return sorted(
        (json.loads(path.read_text()) for path in folder.glob("trial-*.json")),
        key=lambda trial: trial["trial"],
    )


def provenance() -> None:
    for arm in ARMS:
        trials = _trials(HERE / arm)
        config = trials[0]["config"]
        complete = [t for t in trials if t["interrupted"] is None]
        jobs = [job for t in complete for job in t["jobs"]]
        served = Counter(model for job in jobs for model in job["served_models"])
        builds = {fp for job in jobs for fp in job["fingerprints"]}
        per_job = Counter(len(job["fingerprints"]) for job in jobs)
        shas = sorted({t["environment"]["git_sha"][:7] for t in trials})
        dirty = any(t["environment"]["git_dirty"] for t in trials)
        price = config["price"]
        print(
            f"  {arm}: agent digest {config['agent_digest'][:16]}, model {config['model']} on "
            f"{config['provider']}; build {', '.join(shas)}, {'dirty' if dirty else 'clean'}"
        )
        print(
            f"    complete trials: {len(complete)}; jobs answered by {dict(served)}; "
            f"{len(builds)} distinct builds; jobs by how many builds answered them: "
            f"{dict(sorted(per_job.items()))}"
        )
        print(
            f"    price {price['input_per_million_tokens']} / {price['output_per_million_tokens']} "
            f"{price['currency']} per million input / output tokens, from {price['source']} "
            f"({price['retrieved']})"
        )


def trials() -> None:
    print(
        f"  {'arm':>15} {'trial':>5} {'started':>9} {'met':>5} {'solved':>6} {'esc':>4} "
        f"{'failed':>6} {'prompt tok':>10} {'compl tok':>9} {'calls':>5} {'cost $':>8} "
        f"{'wall s':>7} {'limit wait':>10}  interrupted"
    )
    for arm in ARMS:
        for trial in _trials(HERE / arm):
            m = trial["metrics"]
            c = m["counts"]
            failed = c["failed_task"] + c["failed_infra"] + c["failed_harness"]
            wall = m["batch_wall_clock_seconds"]
            print(
                f"  {arm:>15} {trial['trial']:>5} {trial['started_at'][11:19]:>9} "
                f"{c['matched_expectation']:>2}/{c['jobs']:<2} {c['solved']:>6} {c['escalated']:>4} "
                f"{failed:>6} {m['prompt_tokens']:>10,} {m['completion_tokens']:>9,} "
                f"{m['llm_calls']:>5} {m['cost_usd'] or 0:>8.4f} {wall:>7.1f} "
                f"{m['retry_wait_seconds'] / wall if wall else 0:>10.0%}  "
                f"{trial['interrupted'] or '-'}"
            )


def tasks() -> None:
    complete = {
        arm: [t for t in _trials(HERE / arm) if t["interrupted"] is None] for arm in ARMS
    }
    ids = sorted({job["task_id"] for t in complete[ARMS[0]] for job in t["jobs"]})
    print(f"  {'task':>20}  " + "  ".join(f"{arm + ' outcomes':>32}  {'tokens':>7}" for arm in ARMS))
    for task in ids:
        cells = []
        for arm in ARMS:
            jobs = [job for t in complete[arm] for job in t["jobs"] if job["task_id"] == task]
            outcomes = Counter(
                job["outcome"] if job["outcome"] != "failed" else f"failed:{job['failure_mode']}"
                for job in jobs
            )
            tokens = median(job["prompt_tokens"] + job["completion_tokens"] for job in jobs)
            cells.append(f"{str(dict(outcomes)):>32}  {tokens:>7,.0f}")
        print(f"  {task:>20}  " + "  ".join(cells))


def against_m16() -> None:
    """Each task's tokens in m16's real run of the same prompt and model, two days earlier."""
    m16 = {}
    for path in (HERE.parents[3] / "backend" / "bench" / "recordings" / "v1").glob("*.json"):
        recording = json.loads(path.read_text())
        calls = recording["calls"]
        m16[recording["task_id"]] = (
            sum(c["prompt_tokens"] + c["completion_tokens"] for c in calls),
            len(calls),
            recording["recorded_at"][:10],
        )
    first = {arm: [t for t in _trials(HERE / arm) if t["trial"] == 1][0] for arm in ARMS}
    print(f"  {'task':>20}  {'m16, the same prompt':>20}  {'m22 baseline':>14}  {'m22 short':>12}")
    totals = [0, 0, 0]
    for task in sorted(m16):
        row = [m16[task][:2]]
        for arm in ARMS:
            [job] = [j for j in first[arm]["jobs"] if j["task_id"] == task]
            row.append((job["prompt_tokens"] + job["completion_tokens"], job["llm_calls"]))
        for index, (tokens, _) in enumerate(row):
            totals[index] += tokens
        cells = [f"{tokens:>9,} in {calls:>2} calls" for tokens, calls in row]
        print(f"  {task:>20}  {cells[0]:>20}  {cells[1]:>14}  {cells[2]:>12}")
    same = sum(
        1
        for task in m16
        if m16[task][0]
        == sum(
            j["prompt_tokens"] + j["completion_tokens"]
            for j in first["baseline"]["jobs"]
            if j["task_id"] == task
        )
    )
    dates = sorted({value[2] for value in m16.values()})
    print(
        f"  totals: m16 {totals[0]:,} (recorded {', '.join(dates)}), m22 baseline {totals[1]:,}, "
        f"m22 short {totals[2]:,}; the baseline matched m16 to the token on {same} of {len(m16)} tasks"
    )


def comparison() -> None:
    record = json.loads((HERE / "comparison.json").read_text())
    print(f"  rounds paired {record['rounds']}, {record['pairs']} paired runs of {record['jobs']} jobs")
    print(f"  config changes: {sorted(record['config_changes'])}; undeclared: {record['unexpected_changes']}")
    print(f"  environment changes: {record['environment_changes'] or 'none'}")
    for line in record["verdict"]:
        print(f"  - {line}")


def replays() -> None:
    for arm in ARMS:
        [original] = [t for t in _trials(HERE / arm) if t["trial"] == 1]
        [replayed] = _trials(HERE / "replayed" / arm)
        by_id = {job["job_id"]: job for job in replayed["jobs"]}
        same = [
            job["job_id"]
            for job in original["jobs"]
            if (
                job["outcome"],
                job["failure_mode"],
                job["prompt_tokens"],
                job["completion_tokens"],
                job["llm_calls"],
            )
            == (
                by_id[job["job_id"]]["outcome"],
                by_id[job["job_id"]]["failure_mode"],
                by_id[job["job_id"]]["prompt_tokens"],
                by_id[job["job_id"]]["completion_tokens"],
                by_id[job["job_id"]]["llm_calls"],
            )
        ]
        diverged = replayed["metrics"]["counts"]["failed_harness"]
        print(
            f"  {arm}: {len(same)} of {len(original['jobs'])} jobs replayed to the same outcome, "
            f"tokens and model calls; {diverged} replay divergences; digest "
            f"{'the same' if replayed['config']['agent_digest'] == original['config']['agent_digest'] else 'DIFFERENT'}"
        )


def main() -> None:
    print("what each arm ran with:")
    provenance()
    print()
    print("every trial, in the order the rounds ran:")
    trials()
    print()
    print("each task in each arm, over its complete trials: outcomes, and median tokens a job:")
    tasks()
    print()
    print("the first round's tokens per task against m16's run of the unchanged prompt:")
    against_m16()
    print()
    print("the comparison the bench generated (comparison.json):")
    comparison()
    print()
    print("each arm's first trial replayed from its own recorded responses:")
    replays()


if __name__ == "__main__":
    main()
