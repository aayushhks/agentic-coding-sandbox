"""Recompute every number about the real agent's run through the fleet from the committed records.

    python3 docs/results/bench/real-fleet/analysis.py

Standard library only. It prints what the run ran with, the batch, every job with its attempts and
where its time went, every job that didn't meet its expectation and why, and the same tasks on the
same model run sequentially (M16's trial and M22's baseline) beside it.
"""

import json
import statistics
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
TRIAL = HERE / "fleet-4w-real-qwen3.8-27b" / "trial-1.json"
SEQUENTIAL = {
    "m16": BENCH / "sequential-real-qwen3.8-27b" / "trial-1.json",
    "m22": BENCH / "m22" / "baseline" / "trial-1.json",
}
# the key's limit on this model, from the provider's own messages
TOKENS_PER_MINUTE = 8000

median = statistics.median


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _service(job: dict) -> float:
    return job["finished_at"] - job["claimed_at"]


def provenance(trial: dict) -> None:
    config, env = trial["config"], trial["environment"]
    jobs = trial["jobs"]
    builds = Counter(fp for job in jobs for fp in job["fingerprints"])
    per_job = Counter(len(job["fingerprints"]) for job in jobs)
    baseline = _load(SEQUENTIAL["m22"])["config"]["agent_digest"]
    print(
        f"  commit {env['git_sha'][:7]}, {'dirty' if env['git_dirty'] else 'clean'}; "
        f"{config['executor']} executor, {config['workers']} workers: {config['topology']}"
    )
    print(f"  execution: {json.dumps(config['execution'])}")
    print(
        f"  {env['cpu_model']}, {env['logical_cpus']} logical CPUs, {env['memory_total_gib']} GiB, "
        f"{env['os']}, kernel {env['kernel']}, Python {env['python']}"
    )
    print(
        f"  model {config['model']} on {config['provider']}; answered by "
        f"{dict(Counter(m for job in jobs for m in job['served_models']))}; {len(builds)} builds; "
        f"jobs by how many builds answered them: {dict(sorted(per_job.items()))}"
    )
    same = "the same as" if config["agent_digest"] == baseline else "NOT the same as"
    print(f"  agent digest {config['agent_digest'][:16]}, {same} M22's baseline")
    print(f"  started {trial['started_at']}, interrupted: {trial['interrupted']}")


def batch(name: str, trial: dict) -> None:
    m, jobs = trial["metrics"], trial["jobs"]
    c = m["counts"]
    wall = m["batch_wall_clock_seconds"]
    busy = sum(_service(job) for job in jobs)
    waited = sum(job["retry_wait_seconds"] for job in jobs)
    tokens = m["prompt_tokens"] + m["completion_tokens"]
    print(
        f"  {name:>5}: {c['matched_expectation']}/{c['jobs']} met; {c['solved']} solved, "
        f"{c['escalated']} escalated, {c['failed_task']} failed (task), {c['failed_infra']} "
        f"(infra), {c['failed_harness']} (harness); {wall:,.1f} s, "
        f"{m['tasks_per_minute']:.2f} tasks/min"
    )
    print(
        f"         {m['prompt_tokens']:,} + {m['completion_tokens']:,} tokens in {m['llm_calls']} "
        f"calls, {tokens / (wall / 60):,.0f} tokens a minute against the key's "
        f"{TOKENS_PER_MINUTE:,}; cost at list price ${m['cost_usd'] or 0:.4f}"
    )
    print(
        f"         waiting on the rate limit: {waited:,.1f} s, {waited / busy:.0%} of the workers' "
        f"busy time ({busy:,.1f} s); queue wait p50 {m['queue_wait_seconds']['p50']:.1f} s; "
        f"service p50 / max {m['service_time_seconds']['p50']:.1f} / "
        f"{m['service_time_seconds']['max']:.1f} s; utilization {m['utilization']:.3f}"
    )


def jobs(trial: dict) -> None:
    print(
        f"  {'task':>20} {'worker':>6} {'tries':>5} {'claimed':>8} {'service':>8} {'limit':>7} "
        f"{'model':>6} {'tokens':>7} {'calls':>5}  outcome"
    )
    for job in sorted(trial["jobs"], key=lambda job: job["claimed_at"]):
        model = job["output"]["model_seconds"] if job["output"] else None
        print(
            f"  {job['task_id']:>20} {job['worker']:>6} {job['attempts']:>5} "
            f"{job['claimed_at']:>7.1f}s {_service(job):>7.1f}s {job['retry_wait_seconds']:>6.1f}s "
            f"{'-' if model is None else f'{model:.1f}s':>6} "
            f"{job['prompt_tokens'] + job['completion_tokens']:>7,} {job['llm_calls']:>5}  "
            f"{job['outcome']}{'' if job['matched_expectation'] else ' (unmet)'}"
            f"{'' if job['failure_mode'] is None else ', ' + job['failure_mode']}"
        )


def attempts(trial: dict) -> None:
    more = [
        job
        for job in trial["jobs"]
        if job["attempts"] > 1
        or any(a["ended_by"] != "published" for a in job["attempt_history"] or [])
    ]
    if not more:
        print("  every job finished on its first attempt, ended by its own publish")
    for job in more:
        print(f"  {job['task_id']}:")
        for a in job["attempt_history"] or []:
            print(
                f"    attempt {a['attempt']} on {a['worker']}: claimed {a['claimed_at']:.1f} s, "
                f"ended {a['ended_at']} by {a['ended_by']}, error {a['error']}"
            )


def unmet(trial: dict) -> None:
    missed = [job for job in trial["jobs"] if not job["matched_expectation"]]
    if not missed:
        print("  every job met its expectation")
    for job in missed:
        output = job["output"] or {}
        tests = output.get("tests") or {}
        print(
            f"  {job['task_id']} (expected {job['expected']}): {job['outcome']}, "
            f"{job['failure_kind']} / {job['failure_mode']}, ended {job['termination_reason']}, "
            f"{job['llm_calls']} calls, {job['retry_wait_seconds']:.1f} s on the rate limit of "
            f"{_service(job):.1f} s"
        )
        if output:
            print(f"    answer: {output['answer'][:200]!r}")
            print(f"    escalation: {output['escalation_reason'][:200]!r}")
            print(f"    tests: passed {tests.get('passed')}, exit {tests.get('exit_code')}")
            print(f"    test output tail: {str(tests.get('output', ''))[-300:]!r}")


def against_sequential(trial: dict) -> None:
    others = {name: _load(path) for name, path in SEQUENTIAL.items()}
    print(f"  {'task':>20} {'fleet':>16} {'m22 sequential':>16} {'m16 sequential':>16}")
    by = {
        name: {job["task_id"]: job for job in t["jobs"]}
        for name, t in [("fleet", trial), *others.items()]
    }

    def cell(job: dict | None) -> str:
        if job is None:
            return "-"
        tokens = job["prompt_tokens"] + job["completion_tokens"]
        mark = "" if job["matched_expectation"] else "!"
        return f"{job['outcome'][:4]}{mark} {tokens:>7,}"

    for task in sorted(by["fleet"]):
        print(
            f"  {task:>20} {cell(by['fleet'].get(task)):>16} {cell(by['m22'].get(task)):>16} "
            f"{cell(by['m16'].get(task)):>16}"
        )
    same = sum(
        by["fleet"][t]["outcome"] == by["m22"][t]["outcome"] for t in by["fleet"] if t in by["m22"]
    )
    print(f"  outcome the same as M22's sequential run on {same} of {len(by['fleet'])} tasks")


def main() -> None:
    if not TRIAL.exists():
        print(f"no record at {TRIAL.relative_to(BENCH.parents[2])}: the run hasn't been recorded")
        return
    trial = _load(TRIAL)
    print("what it ran with")
    provenance(trial)
    print("\nthe batch, beside the same tasks run sequentially on the same model")
    batch("fleet", trial)
    for name, path in SEQUENTIAL.items():
        batch(name, _load(path))
    print("\nevery job, in the order it was claimed (times from the submit, on Postgres's clock)")
    jobs(trial)
    print("\njobs with more than one attempt, or an attempt that didn't end in its own publish")
    attempts(trial)
    print("\njobs that didn't meet their expectation")
    unmet(trial)
    print("\noutcome and tokens per task")
    against_sequential(trial)


if __name__ == "__main__":
    main()
