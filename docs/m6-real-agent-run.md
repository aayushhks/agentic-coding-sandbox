# M6 — First Real Agent Run

The first end-to-end run of the agent against the full `v1` benchmark using a **real**
LLM — Llama 3.3 70B (`llama-3.3-70b-versatile`) via Groq — rather than the mock provider.
Everything up to this point (the loop, sandbox, benchmark, eval harness, persistence) was
validated against scripted mocks; M6 is where it meets an actual model.

Run date: 2026-05-30 · benchmark `v1` (15 tasks) · raw results:
[`results/groq-llama-3.3-70b-v1.json`](results/groq-llama-3.3-70b-v1.json).

## Headline

**Solve rate: 86.7% (13/15).** Both failures are `hard` tasks, and they fail for two
*different* reasons — one a subtly wrong solution, one a protocol-level parse breakdown.

| Cut | Solved | Rate |
|---|---|---|
| **Overall** | 13 / 15 | **86.7%** |
| easy | 5 / 5 | 100% |
| medium | 5 / 5 | 100% |
| hard | 3 / 5 | 60% |

| Category | Solved | Rate |
|---|---|---|
| algorithms | 7 / 7 | 100% |
| string_manipulation | 3 / 3 | 100% |
| refactor | 1 / 1 | 100% |
| bugfix | 1 / 2 | 50% |
| data_structures | 1 / 2 | 50% |

Difficulty is the clean predictor here: the model clears every easy and medium task and
loses only on hard ones. The per-category dips (bugfix, data_structures at 50%) are an
artifact of small N — each of those categories has exactly one hard task, and that hard
task is the one that failed.

## How it was run

```bash
LLM_PROVIDER=groq DATABASE_URL="sqlite+aiosqlite:///eval.db" \
  uv run python -m app.eval.cli --label groq-llama-3.3-70b --create-tables
```

- **Model:** `llama-3.3-70b-versatile`, `temperature=0.0`, `max_tokens=1024` per call.
- **Agent:** ReAct loop, `max_iterations=15`, `max_consecutive_malformed=3`, **single
  attempt per task** (no best-of-N, no reflection beyond the core loop).
- **Sandbox:** the `SubprocessSandbox` with network isolation **active** — `unshare --net`
  was available in this environment, verified before the run.
- **Persistence:** SQLite, the self-contained path documented in the README (this cloud
  build environment has no usable Postgres/Docker daemon; the harness is storage-agnostic,
  so the same run persists to Postgres unchanged where a daemon exists).
- **Rate limits:** none hit. Zero `429`s and zero `provider_error`s across all 15 tasks;
  the Groq SDK's default retry was never even exercised into failure, so no extra
  backoff/pacing was needed for a sequential run of this size.

## Cost and latency

| Metric | Value |
|---|---|
| Total tokens | **65,400** (57,325 prompt / 8,075 completion) |
| Avg tokens / task | ~4,360 |
| Avg iterations / task | 4.9 |
| Total wall-clock | 285 s (~4.75 min) |
| Avg wall-clock / task | ~19 s |

Prompt tokens dominate completion ~7:1, which is expected for ReAct: every turn re-sends
the full transcript (system prompt + task + every prior observation), so cost grows roughly
quadratically with iteration count. The two most expensive tasks — `topological_sort`
(11,270 tok, 9 iters) and `merge_sorted` (8,767 tok, 6 iters) — were both *solved*; spend
tracks iteration count, not failure.

Wall-clock is dominated by the sandbox, not the model. Tasks where the agent ran a command
that sat near the 10 s timeout (e.g. `fizzbuzz` at 23 s, `topological_sort` at 56 s) cost far
more seconds than token-heavy-but-fast ones — Groq inference itself is sub-second to a few
seconds per call.

## Tool usage

Aggregate tool calls across the run: `write_file` 27, `run_tests` 16, `finish` 14,
`read_file` 8, `list_dir` 6 (71 total). `finish` is 14 rather than 15 because `lru_cache`
never reached a clean finish (it died on the malformed-tool cap). The agent leans on
`write_file` and `run_tests` and uses `read_file`/`list_dir` sparingly — sensible, since most
tasks start from an empty or near-empty workspace.

## Per-task results

Failures first, then by difficulty.

| Task | Category | Diff | Solved | Iters | Tokens | Sec | Failure mode |
|---|---|---|:--:|--:|--:|--:|---|
| **fix_binary_search** | bugfix | hard | ❌ | 4 | 3,016 | 15.2 | `wrong_solution` |
| **lru_cache** | data_structures | hard | ❌ | 3 | 2,933 | 14.9 | `malformed_tool_call` |
| add_numbers | algorithms | easy | ✅ | 3 | 1,692 | 2.5 | |
| factorial | algorithms | easy | ✅ | 3 | 1,875 | 2.2 | |
| count_vowels | string_manipulation | easy | ✅ | 4 | 2,521 | 2.8 | |
| fizzbuzz | algorithms | easy | ✅ | 5 | 4,507 | 23.2 | |
| reverse_string | string_manipulation | easy | ✅ | 5 | 3,172 | 16.7 | |
| fix_average | bugfix | medium | ✅ | 4 | 2,585 | 10.1 | |
| valid_parentheses | data_structures | medium | ✅ | 5 | 3,798 | 18.9 | |
| roman_to_int | string_manipulation | medium | ✅ | 5 | 3,911 | 19.2 | |
| two_sum | algorithms | medium | ✅ | 6 | 5,521 | 27.7 | |
| merge_sorted | algorithms | medium | ✅ | 6 | 8,767 | 44.2 | |
| fibonacci_fast | algorithms | hard | ✅ | 5 | 4,200 | 3.6 | |
| multi_file_shapes | refactor | hard | ✅ | 7 | 5,632 | 27.3 | |
| topological_sort | algorithms | hard | ✅ | 9 | 11,270 | 56.3 | |

## Failure analysis

### `fix_binary_search` — `wrong_solution` (an over-correction the agent couldn't see)

The task ships a binary search with a deliberate infinite-loop bug: on the `arr[mid] < target`
branch it does `lo = mid` instead of `lo = mid + 1`. The agent read the file, **correctly
diagnosed the infinite loop**, and rewrote it — but "symmetrized" the fix and changed *both*
branches:

```python
def binary_search(arr, target):
    lo, hi = 0, len(arr)          # hi is an EXCLUSIVE upper bound
    while lo < hi:
        mid = (lo + hi) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            lo = mid + 1          # ✅ the real fix
        else:
            hi = mid - 1          # ❌ regression: should stay `hi = mid`
    return -1
```

Because `hi` starts at `len(arr)` (exclusive), the upper branch must be `hi = mid`, not
`hi = mid - 1`. The agent's version skips the boundary element. The hidden suite catches it
immediately:

```
assert binary_search([1, 3, 5, 7, 9], 7) == 3
E   assert -1 == 3
```

Trace: `7` lives at index 3. lo=0,hi=5 → mid=2 (`5<7`) → lo=3; mid=4 (`9>7`) → `hi = mid-1 = 3`;
now `lo == hi == 3`, loop exits, returns `-1`. The element was excluded by the off-by-one.

The deeper issue isn't the off-by-one — it's that **the agent finished without ever
verifying.** It called `run_tests` against the workspace, got `exit_code=5 / "no tests ran"`
(the hidden suite isn't present during solving — by design), read that uninformative result
as "nothing to check," and called `finish`. It treated *absence of test feedback* as
*passing tests*. On a one-liner that would be harmless; on a subtle fix it's fatal.

### `lru_cache` — `malformed_tool_call` (the JSON protocol vs. a large code payload)

`lru_cache` is the most code-heavy task in the suite (a full `LRUCache` class with `get`/`put`
and eviction). The agent tried to emit the whole implementation inline as a `write_file`
tool call, and the JSON parser rejected it **three turns in a row**, hitting the
`max_consecutive_malformed=3` cap:

```
response was not valid JSON: Extra data: line 1 column 1084 (char 1083)
response was not valid JSON: Extra data: line 1 column 1078 (char 1077)
response was not valid JSON: Extra data: line 1 column 651  (char 650)
```

Inspecting the raw responses, the cause is precise and almost petty: **all three attempts
emit a complete, valid `write_file` tool call followed by a single stray `}`** — the model
closes the JSON with one brace too many.

```
attempt 0: len=1084  first object (write_file) parses cleanly, ends at char 1083, trailing='}'
attempt 1: len=1078  first object (write_file) parses cleanly, ends at char 1077, trailing='}'
attempt 2: len=651   first object (write_file) parses cleanly, ends at char  650, trailing='}'
```

"Extra data" is exactly that trailing brace: `json.loads` consumes the whole object, then
finds one more `}` and rejects the response. The `_extract_json` salvage heuristic (slice from
the first `{` to the last `}`) is no help here — the stray brace *is* the last `}`, so the
slice still includes it. At temperature 0 the model regenerated the identical over-closed
output three times running, tripping the malformed cap. The loop terminates with nothing
written, and the hidden tests then fail at collection with
`ModuleNotFoundError: No module named 'solution'` (the file was never created).

This is the single most actionable finding in the run, precisely because it's **not a
reasoning failure** — the model wrote a complete, plausibly-correct `LRUCache`; one excess
character threw it all away. Candidate fixes for a later milestone, in rough order of bang for
the buck:

- **A balanced-brace extractor** that stops at the first *complete* object (track depth from the
  first `{`, return at depth 0) instead of slicing to the last `}`. This alone recovers all
  three attempts here perfectly.
- **A fenced-code payload channel** so large file bodies don't have to survive JSON escaping at
  all — the brittlest part of the current single-object protocol.
- **A reformat retry** that shows the model the exact error position. The loop already feeds the
  parse error back, but the generic message isn't enough for the model to self-correct at
  temperature 0; a pointed "you emitted a trailing `}` at column N" might be.

## What worked: the agent writes (and debugs) its own tests

The bright spot. On harder tasks the agent recognized the empty workspace, **authored its own
test file, ran it, and iterated until green** before finishing:

- **`fibonacci_fast`** (the naive-recursion timeout trap): the agent wrote a dynamic-programming
  `fib` from the start — never triggering the timeout the task is designed to punish — then
  wrote its own tests, ran them (`2 passed`), and finished. 5 iterations, 3.6 s. The adversarial
  task landed flat.
- **`topological_sort`**: the agent implemented a DFS topo-sort, wrote a test file, ran it, and
  the run **failed on a syntax error in its own test** (`unmatched ')'`). It read the traceback,
  fixed the test, re-ran (`2 passed`), and finished. Self-authored *and* self-repaired its
  verification harness — 9 iterations, the longest clean solve in the run.

## The cross-cutting insight

**Success on this run correlates with whether the agent chose to write its own tests.** The
grading suite is deliberately hidden (the eval must not be teachable-to), so the agent's only
safety net is the one it builds itself — and it builds it *inconsistently*:

- When it wrote tests (`fibonacci_fast`, `topological_sort`, `merge_sorted`), it caught its own
  mistakes and converged.
- When it skipped that step and leaned on the absent hidden suite (`add_numbers` got away with
  it because the code was trivially correct; `fix_binary_search` did **not**), it finished
  blind. The `add_numbers` and `fix_binary_search` traces are nearly identical in shape — write,
  `run_tests` → "no tests ran", `finish` — and differ only in whether the unverified code
  happened to be right.

So the headline 86.7% is, in part, luck about *which* unverified solutions were correct. A
single intervention — **make the agent author and pass its own tests before `finish` is
allowed**, and stop treating "no tests ran" as success — would likely convert
`fix_binary_search` and harden the rate against the next subtle bug. That's a concrete
candidate for M7/loop hardening.

## Adversarial-task scorecard

The benchmark plants three traps; the model went 2-for-3:

| Trap | Task | Outcome |
|---|---|---|
| Naive-recursion timeout | `fibonacci_fast` | ✅ Beaten — wrote DP from the start, never timed out |
| Infinite-loop bug | `fix_binary_search` | ❌ Fixed the loop, introduced an off-by-one, finished unverified |
| Multi-file package refactor | `multi_file_shapes` | ✅ Solved — 7 iterations |

## Honest boundaries

- **Small N.** 15 tasks characterize behavior and failure modes; they are not a statistical
  claim about Llama 3.3 70B's coding ability. The per-category rates especially (1-of-2s) are
  noisy.
- **Single attempt, temperature 0.** One deterministic shot per task. Both failures might well
  resolve under best-of-N or a reflection step — neither is in the loop yet. The 86.7% is a
  floor for this model under this minimal harness, not a ceiling.
- **Free-tier model.** Llama 3.3 70B is weaker than frontier models; the harness is
  model-agnostic, so a stronger model is a config change, not a code change. Re-running this
  exact benchmark across models is M7.
- **One run.** No variance estimate. At temperature 0 the solves are largely stable, but the
  `lru_cache` parse failure in particular is the kind of thing that could differ run to run.

## Reproducing

```bash
cd backend
uv sync
LLM_PROVIDER=groq GROQ_API_KEY=... DATABASE_URL="sqlite+aiosqlite:///eval.db" \
  uv run python -m app.eval.cli --label groq-llama-3.3-70b --create-tables
```

The full per-step traces (reasoning, raw output, tool calls, observations, per-step tokens)
for every task are persisted to the `task_results.trace` JSON column; the committed
[`results/groq-llama-3.3-70b-v1.json`](results/groq-llama-3.3-70b-v1.json) carries the
run-level and per-task aggregates.
