# fleet-4w-real-qwen3.8-27b against m22-baseline

Rounds paired: [1]; 18 paired runs of 18 jobs. Trials without a pair: {'baseline': [2, 3], 'candidate': []}.

## Verdict

- Expectations met: 100.0% of runs without the change, 77.8% with it, a change of -22.2 points (95% interval -44.4 to -5.6) over 18 paired runs of 18 jobs. 0 met it only with the change and 4 only without (exact McNemar p = 0.125, counting each paired run on its own): worse.
- Tokens per job: 6,552 → 4,039 tokens (-2,513, -38.4%; 95% interval -5,660 to +80): no detectable change.
- Cost per job at the pinned price: 0.00853 → 0.00556 USD (-0.00297, -34.8%; 95% interval -0.00663 to +0.00015): no detectable change.
- Model calls per job: 5.17 → 3.72 calls (-1.44, -28.0%; 95% interval -2.89 to -0.22): better.
- Time waiting on the model per job: 5.17 → 3.74 s (-1.43, -27.7%; 95% interval -3.11 to +0.07): no detectable change.
- Service time per job: 57.45 → 127.42 s (+69.97, +121.8%; 95% interval +29.91 to +115.35): worse.

## What changed

- `execution`: `None` → `{'mode': 'process', 'policy': {'cpus': 1.0, 'memory_mb': 1024, 'pids': 256, 'tmp_mb': 512, 'timeout_seconds': 600.0, 'egress': []}}`
- `executor`: `sequential` → `fleet`
- `topology`: `single host, one in-process worker` → `single host: 4 fleet worker processes, the fleet api and PostgreSQL 16.13, all on this machine`
- `workers`: `1` → `4`

Environment differences: {'git_sha': [['fa79733f54a330b16dc5946fa077dbaa37fa70e7'], ['cbe7ba9806b0e73fc22900ba94ada4fb2233c86d']], 'cpu_model': [['Intel(R) Xeon(R) Processor @ 2.10GHz'], ['Intel(R) Xeon(R) Processor @ 2.80GHz']]}

## Tasks whose outcomes moved

| Task | Expected | Runs | Baseline | Candidate |
|---|---|---|---|---|
| factorial | solve | 1 | {'solved': 1} (1 met) | {'failed: provider_error': 1} (0 met) |
| fizzbuzz | solve | 1 | {'solved': 1} (1 met) | {'failed: provider_error': 1} (0 met) |
| merge_sorted | solve | 1 | {'solved': 1} (1 met) | {'failed: provider_error': 1} (0 met) |
| two_sum | solve | 1 | {'solved': 1} (1 met) | {'failed: provider_error': 1} (0 met) |
