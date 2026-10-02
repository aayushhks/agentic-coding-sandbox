"""Recompute every M21 scaling number from the committed trial records.

    python3 docs/results/bench/m21/analysis.py

Standard library only. For each run it prints the machine and build it ran on, then:

- scaling: throughput per pool size, speedup over the smallest pool in the same round, and queue
  wait and utilization, as the milestone asks;
- efficiency: speedup per worker split into two measured factors, how much longer each job took
  (service time against the smallest pool's) and how much of the workers' time sat idle, with the
  idle time split into the wait for a first job, the gaps between a publish and the next claim,
  and the end of the batch after a worker's last job;
- cpu: how busy the host was, how often a runnable task waited for a cpu, the cpu each job cost
  and which process tree spent it, and the throughput at which the host's cpus would all be busy;
- database: the store calls on each job's path, their share of its service time, and the gap
  between a worker publishing one job and claiming its next;
- the batch's end: its length against the same jobs dealt to the pool with no time lost between
  them, and, for the largest pool, when its workers ran out of work and which job ended it.

Then the most cpu Postgres used in any trial; the start-up race the first runs fell into (when the
api answered against when each worker came up, and those first runs against the same runs made once
the bench waited for its workers); and M16's real-model trial, for the provider's rate limit.
"""

import heapq
import json
import statistics
from collections.abc import Callable, Iterable
from itertools import pairwise
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = {
    "zero": "zero latency, each job in its worker's process",
    "past-cores": "zero latency past the core count, added after the zero run",
    "recorded": "recorded latency, each job in its worker's process",
    "containers": "recorded latency, each job in a container of its own",
}
# the store calls a job makes on its way through a worker, as against an idle worker's polling
PER_JOB = ("claim", "start", "heartbeat", "record_execution", "publish")
PROCESSES = ("workers", "api", "postgres", "docker", "harness")

median = statistics.median


def _percentile(values: list[float], pct: float) -> float:
    # linear interpolation between closest ranks, the bench's own method
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _spread(values: Iterable[float], spec: str = ".2f") -> str:
    values = list(values)
    low, mid, high = (format(value, spec) for value in (min(values), median(values), max(values)))
    return f"{mid} [{low} to {high}]"


def _pools(run: Path) -> dict[int, list[dict]]:
    """Each pool's trials, by worker count, in trial order."""
    pools = {}
    for folder in sorted(path for path in run.iterdir() if path.is_dir()):
        trials = [json.loads(path.read_text()) for path in folder.glob("trial-*.json")]
        if trials:
            pools[trials[0]["config"]["workers"]] = sorted(trials, key=lambda t: t["trial"])
    return dict(sorted(pools.items()))


def _each(trials: list[dict], value: Callable[[dict], float]) -> list[float]:
    return [value(trial) for trial in trials]


def _service(trial: dict) -> list[float]:
    return [job["finished_at"] - job["claimed_at"] for job in trial["jobs"]]


def _rounds(pools: dict[int, list[dict]], workers: int) -> list[tuple[dict, dict]]:
    """Each trial of a pool beside the smallest pool's trial from the same round."""
    return list(zip(pools[min(pools)], pools[workers], strict=True))


def machine(pools: dict[int, list[dict]]) -> None:
    records = [trial for trials in pools.values() for trial in trials]
    first = records[0]["environment"]
    shas = ", ".join(sorted({record["environment"]["git_sha"][:7] for record in records}))
    dirty = any(record["environment"]["git_dirty"] for record in records)
    config = records[0]["config"]
    print(
        f"  {first['cpu_model']}, {first['logical_cpus']} logical cpus "
        f"({'virtualized' if first['virtualized'] else 'bare metal'}), "
        f"{first['memory_total_gib']} GiB, {first['os']}, kernel {first['kernel']}, "
        f"python {first['python']}"
    )
    print(f"  {pools[max(pools)][0]['config']['topology']}")
    print(
        f"  build {shas}, {'dirty' if dirty else 'clean'} checkout; {config['tasks']} jobs a "
        f"batch, seed {config['seed']}; trials per pool: "
        + ", ".join(f"{workers} workers {len(trials)}" for workers, trials in pools.items())
    )


def scaling(pools: dict[int, list[dict]]) -> None:
    print(
        f"  {'workers':>7}  {'tasks/min':>24}  {'speedup':>20}  {'queue p50 s':>11}  "
        f"{'queue p95 s':>11}  {'utilization':>11}"
    )
    for workers, trials in pools.items():
        speedups = [
            trial["metrics"]["tasks_per_minute"] / base["metrics"]["tasks_per_minute"]
            for base, trial in _rounds(pools, workers)
        ]
        print(
            f"  {workers:>7}  "
            f"{_spread(_each(trials, lambda t: t['metrics']['tasks_per_minute']), '.1f'):>24}  "
            f"{_spread(speedups):>20}  "
            f"{median(_each(trials, lambda t: t['metrics']['queue_wait_seconds']['p50'])):>11.2f}  "
            f"{median(_each(trials, lambda t: t['metrics']['queue_wait_seconds']['p95'])):>11.2f}  "
            f"{median(_each(trials, lambda t: t['metrics']['utilization'])):>11.3f}"
        )


def _idle(trial: dict) -> tuple[float, float, float]:
    """The workers' idle time as shares of all their time: before a first job, between jobs, after
    a last one. With the busy share, they add up to one."""
    wall = trial["metrics"]["batch_wall_clock_seconds"]
    by_worker: dict[str, list[dict]] = {}
    for job in trial["jobs"]:
        by_worker.setdefault(job["worker"], []).append(job)
    first = between = last = 0.0
    for jobs in by_worker.values():
        jobs.sort(key=lambda job: job["claimed_at"])
        first += jobs[0]["claimed_at"]
        between += sum(b["claimed_at"] - a["finished_at"] for a, b in pairwise(jobs))
        last += wall - jobs[-1]["finished_at"]
    # a worker that never got a job sat idle the whole batch
    idle_workers = trial["config"]["workers"] - len(by_worker)
    capacity = trial["config"]["workers"] * wall
    return first / capacity, between / capacity, (last + idle_workers * wall) / capacity


def efficiency(pools: dict[int, list[dict]]) -> None:
    print(
        f"  {'workers':>7}  {'efficiency':>10}  {'= service':>9}  {'x busy':>7}  "
        f"{'service mean s':>14}  {'idle first':>10}  {'between':>7}  {'after last':>10}"
    )
    for workers in pools:
        rounds = _rounds(pools, workers)
        base_workers = min(pools)
        # efficiency = speedup per worker = (service before / service after) x (busy after / before)
        efficiencies, service, busy = [], [], []
        for base, trial in rounds:
            speedup = trial["metrics"]["tasks_per_minute"] / base["metrics"]["tasks_per_minute"]
            efficiencies.append(speedup * base_workers / workers)
            service.append(sum(_service(base)) / sum(_service(trial)))
            busy.append(trial["metrics"]["utilization"] / base["metrics"]["utilization"])
        idle = [_idle(trial) for _, trial in rounds]
        print(
            f"  {workers:>7}  {median(efficiencies):>10.3f}  {median(service):>9.3f}  "
            f"{median(busy):>7.3f}  "
            f"{median(sum(_service(t)) / len(t['jobs']) for _, t in rounds):>14.3f}  "
            f"{median(share[0] for share in idle):>10.1%}  "
            f"{median(share[1] for share in idle):>7.1%}  "
            f"{median(share[2] for share in idle):>10.1%}"
        )


def cpu(pools: dict[int, list[dict]]) -> None:
    containers = any(job.get("execution") for job in pools[min(pools)][0]["jobs"])
    names = (*PROCESSES, *(["in containers"] if containers else []), "other")
    print(
        f"  {'workers':>7}  {'host busy':>9}  {'cpu wait':>8}  {'steal':>5}  {'cpu s/job':>22}  "
        f"{'ceiling/min':>11}  " + "  ".join(f"{name:>13}" for name in names)
    )
    for workers, trials in pools.items():
        jobs = len(trials[0]["jobs"])
        used = [trial["resources"] for trial in trials]
        per_job = [spent["host_busy_cpu_seconds"] / jobs for spent in used]
        # the rate at which every cpu on the host would be busy, at this much cpu a job
        ceilings = [60 * spent["cpus"] / each for spent, each in zip(used, per_job, strict=True)]
        shares: dict[str, float | None] = {}
        for name in PROCESSES:
            measured = [spent["processes"].get(name) for spent in used]
            known = [value for value in measured if value is not None]
            shares[name] = median(value / jobs for value in known) if len(known) == len(used) else None
        inside = [
            sum((job.get("execution") or {}).get("usage", {}).get("cpu_seconds", 0.0) for job in t["jobs"])
            for t in trials
        ]
        if containers:
            # the task's own cpu inside its container, which no tree on the host reaches
            shares["in containers"] = median(value / jobs for value in inside)
        others = [
            (spent["host_busy_cpu_seconds"] - sum(v for v in spent["processes"].values() if v) - box)
            / jobs
            for spent, box in zip(used, inside, strict=True)
        ]
        shares["other"] = median(others)
        waits = [spent["cpu_wait_fraction"] for spent in used]
        print(
            f"  {workers:>7}  {median(spent['host_busy_fraction'] for spent in used):>9.1%}  "
            f"{'-' if None in waits else format(median(waits), '.1%'):>8}  "
            f"{median(spent['host_steal_fraction'] for spent in used):>5.1%}  "
            f"{_spread(per_job, '.3f'):>22}  {median(ceilings):>11.1f}  "
            + "  ".join(
                f"{'-' if shares[name] is None else format(shares[name], '.3f'):>13}"
                for name in names
            )
        )


def _gaps(trial: dict) -> list[float]:
    """Each worker's time between publishing a job and claiming its next, on Postgres's clock."""
    by_worker: dict[str, list[dict]] = {}
    for job in trial["jobs"]:
        by_worker.setdefault(job["worker"], []).append(job)
    return [
        later["claimed_at"] - earlier["finished_at"]
        for jobs in by_worker.values()
        for earlier, later in pairwise(sorted(jobs, key=lambda job: job["claimed_at"]))
    ]


def database(pools: dict[int, list[dict]]) -> None:
    print(
        f"  {'workers':>7}  {'db ms/job':>9}  {'of service':>10}  {'claim ms':>8}  "
        f"{'claim max ms':>12}  {'publish ms':>10}  {'gap p50 ms':>10}  {'gap p95 ms':>10}  "
        f"{'gap max ms':>10}"
    )
    for workers, trials in pools.items():
        jobs = len(trials[0]["jobs"])
        per_job, share, claim, publish, longest = [], [], [], [], []
        for trial in trials:
            calls = trial["database"]
            spent = sum(calls[name]["seconds"] for name in PER_JOB if name in calls)
            per_job.append(spent / jobs)
            share.append(spent / sum(_service(trial)))
            claim.append(calls["claim"]["seconds"] / calls["claim"]["calls"])
            publish.append(calls["publish"]["seconds"] / calls["publish"]["calls"])
            longest.append(calls["claim"]["max_seconds"])
        gaps = [gap for trial in trials for gap in _gaps(trial)]
        print(
            f"  {workers:>7}  {median(per_job) * 1000:>9.2f}  {median(share):>10.2%}  "
            f"{median(claim) * 1000:>8.2f}  {max(longest) * 1000:>12.1f}  "
            f"{median(publish) * 1000:>10.2f}  {_percentile(gaps, 50) * 1000:>10.2f}  "
            f"{_percentile(gaps, 95) * 1000:>10.2f}  {max(gaps) * 1000:>10.1f}"
        )


def _dealt(service: list[float], workers: int) -> float:
    """How long a batch would take with these service times dealt, in this order, each to the first
    worker free, with no time lost between jobs."""
    free = [0.0] * workers
    for seconds in service:
        heapq.heappush(free, heapq.heappop(free) + seconds)
    return max(free)


def tail(pools: dict[int, list[dict]]) -> None:
    print(
        f"  {'workers':>7}  {'wall s':>8}  {'dealt s':>8}  {'fleet s':>8}  {'even split s':>12}  "
        f"{'longest job s':>13}  {'mean job s':>10}"
    )
    for workers, trials in pools.items():
        walls, dealt, even, longest, mean = [], [], [], [], []
        for trial in trials:
            jobs = sorted(trial["jobs"], key=lambda job: job["claimed_at"])
            service = [job["finished_at"] - job["claimed_at"] for job in jobs]
            walls.append(trial["metrics"]["batch_wall_clock_seconds"])
            dealt.append(_dealt(service, workers))
            even.append(sum(service) / workers)
            longest.append(max(service))
            mean.append(statistics.mean(service))
        # what the fleet added: the batch's length beyond the same jobs dealt with nothing in between
        added = [wall - ideal for wall, ideal in zip(walls, dealt, strict=True)]
        print(
            f"  {workers:>7}  {median(walls):>8.2f}  {median(dealt):>8.2f}  {median(added):>8.2f}  "
            f"{median(even):>12.2f}  {median(longest):>13.2f}  {median(mean):>10.2f}"
        )


def batch_end(pools: dict[int, list[dict]]) -> None:
    """When the largest pool's workers ran out of work, and which job ended each of its batches."""
    workers = max(pools)
    print(
        f"  {'trial':>5}  {'first out s':>11}  {'half out s':>10}  {'batch end s':>11}  "
        f"{'the job that ended it'}"
    )
    for trial in pools[workers]:
        done: dict[str, float] = {}
        for job in trial["jobs"]:
            done[job["worker"]] = max(done.get(job["worker"], 0.0), job["finished_at"])
        out = sorted(done.values())
        jobs = sorted(trial["jobs"], key=lambda job: job["claimed_at"])
        last = max(jobs, key=lambda job: job["finished_at"])
        print(
            f"  {trial['trial']:>5}  {out[0]:>11.1f}  {median(out):>10.1f}  {out[-1]:>11.1f}  "
            f"{last['task_id']}, claimed {jobs.index(last) + 1} of {len(jobs)} at "
            f"{last['claimed_at']:.2f} s, ran {last['finished_at'] - last['claimed_at']:.2f} s"
        )


def postgres() -> None:
    """The most of one cpu the Postgres server used in any trial of any run."""
    shares = [
        (trial["resources"]["processes"]["postgres"] / trial["resources"]["window_seconds"], name)
        for name in RUNS
        if (HERE / name).is_dir()
        for trials in _pools(HERE / name).values()
        for trial in trials
    ]
    share, name = max(shares)
    print(f"  at most {share:.1%} of one cpu, in a {name} trial; {len(shares)} trials in all")


def startup() -> None:
    """When the api first answered against when each worker came up, from startup/startup.json."""
    runs = json.loads((HERE / "startup" / "startup.json").read_text())["runs"]
    print(
        f"  {'workers':>7}  {'api answers s':>13}  {'last worker up s':>16}  "
        f"{'up after the api':>16}  {'last one after the api s':>24}"
    )
    late = 0
    for workers in sorted({run["workers"] for run in runs}):
        mine = [run for run in runs if run["workers"] == workers]
        after = [max(run["workers_up_seconds"]) - run["api_healthy_seconds"] for run in mine]
        count = sum(run["workers_up_after_api"] for run in mine)
        late += count
        print(
            f"  {workers:>7}  {median(run['api_healthy_seconds'] for run in mine):>13.2f}  "
            f"{median(max(run['workers_up_seconds']) for run in mine):>16.2f}  "
            f"{f'{count} of {workers * len(mine)}':>16}  {_spread(after):>24}"
        )
    print(f"  in all, {late} of {sum(run['workers'] for run in runs)} workers came up after the api")


def provider() -> None:
    """M16's real-model trial: its wait on the rate limit, and the tokens a task used."""
    trial = json.loads((HERE.parent / "sequential-real-qwen3.8-27b" / "trial-1.json").read_text())
    metrics = trial["metrics"]
    tokens = metrics["prompt_tokens"] + metrics["completion_tokens"]
    tasks = metrics["counts"]["jobs"]
    wall, waited = metrics["batch_wall_clock_seconds"], metrics["retry_wait_seconds"]
    # the key's limit, 8,000 tokens a minute for this model, as m16 recorded it from groq's headers
    print(
        f"  {trial['config']['model']}: {waited:.1f} s of a {wall:.1f} s batch waiting on the rate "
        f"limit ({waited / wall:.0%}); {tokens:,} tokens for {tasks} tasks, {tokens / tasks:,.0f} a "
        f"task; at 8,000 tokens a minute, {8000 / (tokens / tasks):.2f} tasks a minute"
    )


def _first_claims(trial: dict) -> list[float]:
    """When each worker claimed its first job, in seconds after the batch was submitted."""
    firsts: dict[str, float] = {}
    for job in trial["jobs"]:
        firsts[job["worker"]] = min(firsts.get(job["worker"], job["claimed_at"]), job["claimed_at"])
    return sorted(firsts.values())


def first_runs() -> None:
    """How the first 16-worker batches began, before the bench waited for its workers."""
    trials = _pools(HERE.parent / "m21-before-ready" / "recorded")[16]
    for trial in trials:
        at, busy, runnable = trial["resources"]["timeline"][0]
        claims = _first_claims(trial)
        print(
            f"  trial {trial['trial']}: the first {at:.2f} s {busy:.0%} busy with {runnable} tasks "
            f"runnable; first claims from {claims[0]:.2f} to {claims[-1]:.2f} s"
        )


def readiness() -> None:
    """The first runs, which submitted before every worker was up, against the same runs made once
    the bench waited for its workers, on the same machine."""
    before_root, after_root = HERE.parent / "m21-before-ready", HERE.parent / "m21-previous-vm"
    print(
        f"  {'run':>10}  {'workers':>7}  {'trials':>9}  {'first claim mean s':>21}  "
        f"{'last first claim s':>21}  {'idle first':>17}"
    )
    for name in RUNS:
        if not (before_root / name).is_dir() or not (after_root / name).is_dir():
            continue
        before, after = _pools(before_root / name), _pools(after_root / name)
        for workers in sorted(set(before) & set(after)):
            sides = (before[workers], after[workers])
            means = [median(statistics.mean(_first_claims(t)) for t in side) for side in sides]
            lasts = [median(max(_first_claims(t)) for t in side) for side in sides]
            idle = [median(_idle(t)[0] for t in side) for side in sides]
            print(
                f"  {name:>10}  {workers:>7}  {len(sides[0]):>4} {len(sides[1]):>4}  "
                f"{means[0]:>10.3f} {means[1]:>10.3f}  {lasts[0]:>10.3f} {lasts[1]:>10.3f}  "
                f"{idle[0]:>8.1%} {idle[1]:>8.1%}"
            )


def main() -> None:
    for name, what in RUNS.items():
        run = HERE / name
        if not run.is_dir():
            continue
        pools = _pools(run)
        print(f"{name}: {what}")
        machine(pools)
        print("scaling, medians and ranges over trials; speedup against the smallest pool's trial")
        print("in the same round:")
        scaling(pools)
        print("efficiency, the speedup per worker, split into service time and busy time against")
        print("the smallest pool's, and where the workers' idle time fell:")
        efficiency(pools)
        print("cpu over the batch: host busy, time some runnable task waited for a cpu, cpu seconds")
        print("per job in all and by process tree, and the rate that would keep every cpu busy:")
        cpu(pools)
        print("database: store-call time on each job's path, and each worker's publish-to-claim gap:")
        database(pools)
        print("the batch's length against its jobs' measured service times dealt in claim order to")
        print("the first free worker with no time lost, the fleet's share, and the even split:")
        tail(pools)
        print(f"how the {max(pools)}-worker batches ended: when the workers ran out of work, and the")
        print("job still running:")
        batch_end(pools)
        print()
    print("postgres, across every run:")
    postgres()
    print()
    print("the api's first answer against each worker coming up, the moment the bench used to")
    print("submit, five starts at each pool size (startup/):")
    startup()
    print()
    print("the first 16-worker batches at the model's latency, before the bench waited for its")
    print("workers (../m21-before-ready/recorded):")
    first_runs()
    print()
    print("before and after the bench waited for its workers, both on the vm the first runs used")
    print("(../m21-before-ready, ../m21-previous-vm): medians over trials, each pair before | after,")
    print("of each worker's first claim, mean and latest, and the workers' idle time before it:")
    readiness()
    print()
    print("with the real model, from m16's trial (../sequential-real-qwen3.8-27b):")
    provider()


if __name__ == "__main__":
    main()
