# M22 — Reproducible Evaluation Records: What Every Run Ran With, and Did a Change Help

Every bench run now records what it ran with and what it produced, closely enough that two runs of
the same batch can be set side by side job by job, and the platform can say whether a change made
things better or worse, and by how much. To show it, one sentence was added to the agent's system
prompt and the 18-task set was run with and without it on the real model, interleaved: three rounds
were planned and the provider's daily cap allowed one. The comparison below is the one the bench
generated.

**The demonstration, in one paragraph.** One sentence added to the agent's system prompt — "Keep the
thought field to one short sentence." — cut tokens per job by 46% (6,552 to 3,510; 95% interval
−5,533 to −1,105 tokens), cost per job at list price by 49%, and model calls per job by 20%, with
every task still meeting its expectation. That is over one complete round of the 18-task set on the
real model (`qwen/qwen3.8-27b` on Groq), each arm once; the provider's daily token cap stopped the
two rounds after it. On every one of the 18 tasks the shorter-thoughts run used fewer tokens than
both runs of the unchanged prompt — this one, and M16's two days earlier.

## What a record holds now

Each trial record (`docs/results/bench/<label>/trial-N.json`) carries, beside its metrics:

| The kickoff asked for | Where it is in the record |
|---|---|
| model and version | `config.model` and `config.provider`, what was asked for; each job's `served_models` and `fingerprints`, what the provider says answered, call by call |
| prompt or config hash | `config.agent_digest`: a SHA-256 of every kind of task's rendered system prompt (tool definitions included) and the agent configs; the prompts themselves in `config.system_prompts` |
| container image digest | `config.execution.image_id`, for container runs (M19) |
| task spec version | `config.taskset_version` and `config.taskset_digest` (M16) |
| agent outputs | each job's `output`: its final answer or escalation reason, every file it added, changed or removed, and a unified diff of them, cut at 6,000 characters |
| test results | `output.tests`: whether the hidden tests passed, their exit code and the last 1,500 characters of their output; on a ticket, whether the files it must not touch were left alone |
| latency | each job's queue wait and service time, and `output.model_seconds`, the time it waited on the model |
| token counts | each job's prompt and completion tokens and model calls |
| cost | each job's `cost_usd` at the price pinned in `bench/prices.json`, and the trial's sum; the price carries its source and the day it was read |
| resource limits | `config.execution.policy` for containers, `config.sandbox_config` for the sandbox |
| retry history | each fleet job's `attempt_history`: every attempt, the worker that held it, when it was claimed and ended, how it ended and with what error; each job's `retry_wait_seconds`, the time spent waiting out the provider's rate limit |

The workspace snapshot that the diff comes from is read from the sandbox's directory after the agent
stops and before the grader writes its hidden tests in, and it never follows a symlink: a link the
agent made to a file outside the workspace is recorded as a link, not read.

## How a comparison is made

`bench.cli compare --baseline DIR --candidate DIR --expect FIELD ...` reads two runs' trials and:

- **pairs them job by job.** The arms run interleaved, a round at a time, and the same seed plans
  the same jobs, so job j in round k of one is set against job j in round k of the other. Only
  rounds both completed are paired, and the trials left out are named.
- **checks they differ only in the declared change.** Every config field that differs is listed, and
  one not named with `--expect` fails the comparison (exit 2): a difference nobody declared means
  the change can't be credited with what moved. The system prompts are diffed, and a change of code
  or machine between the runs is reported.
- **measures each change with an interval.** Expectations met, tokens, cost, model calls, model time
  and service time are each a per-job mean in each arm, and the change between them comes with a 95%
  interval from resampling jobs, 10,000 times with a fixed seed. A job is resampled with all its
  rounds, because a model at temperature 0 tends to do the same thing to the same task each round:
  counting its rounds as independent would claim more certainty than there is.
- **gives a verdict per measure**: better or worse when the interval excludes no change, no
  detectable change when it doesn't. Flips of outcome are counted pair by pair, with McNemar's exact
  test beside them, labelled as counting each paired run on its own.
- **names every task whose outcomes moved.**

It writes a JSON record and a markdown report. CI runs it on every push, between the scale smoke's
one- and two-worker runs, declaring the pool size as the change.

## The demonstration: one sentence in the prompt

**Setup.** The 18-task set, seed 1, one sequential worker, `qwen/qwen3.8-27b` on Groq's free tier
(8,000 tokens a minute and 200,000 a day for this model on this key), at commit `fa79733` with a
clean checkout, on the M21 machine (a virtualized 4-vCPU Intel Xeon @ 2.10 GHz). The plan was three
rounds, each arm once a round, the order reversed every round. The change, as the bench diffed it:

```diff
--- baseline/benchmark
+++ candidate/benchmark
@@ -18,3 +18,4 @@
 - Inspect files and run the tests before declaring success.
 - The workspace ships no tests: write your own test file covering the task, then run it with the run_tests tool.
 - Only call finish after run_tests reports passing tests. A result of "no tests ran" does not count as verification.
+- Keep the thought field to one short sentence.
```

(and the same line at the end of the ticket prompt). The two arms' agent digests are `18cb3999…` and
`a8ab7615…`; nothing else in their configs differs, which the comparison checks.

**What ran.** Round 1 completed in both arms. In round 2 the shorter-thoughts trial stopped after 5
of its 18 jobs, at the provider's daily cap, and every trial after that was refused at once. All of
them are kept, marked interrupted, and left out of the pairing. The provider's own words, read with
a probe after the cap: "Rate limit reached for model `qwen/qwen3.8-27b` … on tokens per day (TPD):
Limit 200000, Used 199523, Requested 2513. Please try again in 14m39.552s." The day's budget refills
at about 8,300 tokens an hour, so the two missing rounds needed about 43 more hours of it.

| Arm | Trial | Expectations met | Prompt tokens | Completion tokens | Model calls | Cost at list price | Batch | Waiting on the rate limit |
|---|---|---|---|---|---|---|---|---|
| baseline | 1 | 18 of 18 | 99,417 | 18,510 | 93 | $0.1536 | 1,035.3 s | 90% |
| shorter thoughts | 1 | 18 of 18 | 54,452 | 8,729 | 74 | $0.0785 | 516.4 s | 88% |
| shorter thoughts | 2 | 5 of 5 run | 15,782 | 2,775 | 21 | $0.0237 | 178.4 s | 84% (stopped at the daily cap) |

**The comparison the bench generated** ([comparison.md](results/bench/m22/comparison.md)), over the
18 jobs of the one round both arms completed:

| | Without the sentence | With it | Change | 95% interval | Verdict |
|---|---|---|---|---|---|
| Expectations met | 100% | 100% | 0 points | 0 to 0 | no detectable change |
| Tokens per job | 6,552 | 3,510 | −3,041 (−46.4%) | −5,533 to −1,105 | better |
| Cost per job at list price | $0.00853 | $0.00436 | −$0.00417 (−48.9%) | −$0.00721 to −$0.00179 | better |
| Model calls per job | 5.17 | 4.11 | −1.06 (−20.4%) | −1.78 to −0.50 | better |
| Time waiting on the model per job | 5.17 s | 2.69 s | −2.48 s (−48.0%) | −3.78 to −1.35 s | better |
| Service time per job | 57.45 s | 28.62 s | −28.83 s (−50.2%) | −45.24 to −13.36 s | better |

No task's outcome moved: 15 solved, 2 escalated and 1 expected failure in both arms.

**Where the tokens went.** Completion tokens fell 53% (18,510 to 8,729), which is the sentence doing
what it says. Prompt tokens fell 45% (99,417 to 54,452), because every response goes back into the
conversation as history for every later call, and because there were 20% fewer calls. The service
time halved mostly because fewer tokens meant less time waiting on the 8,000-tokens-a-minute limit:
both arms spent 88–90% of their batch waiting on it, so that saving is this key's, not the model's.

**Is it the sentence, or a long baseline run?** The saving is not even across tasks: `fizzbuzz` went
from 20,791 tokens to 3,498 and `lru_cache` from 20,042 to 4,466, together 60% of the whole saving,
while `fix_average` barely moved (4,038 to 3,946). With one round, the baseline's own run-to-run
spread can't be measured inside this experiment, but M16's real run of the same prompt and model,
two days earlier, is a second sample of it. Set against that run, the baseline matched it to the
token on 7 of the 18 tasks and differed by up to 2.4 times on others (`lru_cache`: 8,507 then
20,042), and the shorter-thoughts run used fewer tokens than both baseline runs on all 18 tasks, and
42% fewer than M16's in all (63,181 against 108,804).

**The records reproduce.** Each arm's first trial was replayed from the responses it recorded,
through the same agent digest: 18 of 18 jobs came out with the same outcome, the same prompt and
completion tokens and the same number of model calls, with no divergence, in both arms
([replayed](results/bench/m22/replayed/)). Anyone can check these numbers again for free; replay
refuses any request the recording didn't see, so a different prompt can't quietly reuse them.

## Honest notes

- **One round, not three.** The daily cap stopped the experiment after one complete round. The
  intervals above come from resampling the 18 jobs, so they cover how the change varies across
  tasks, not how one task varies from run to run; M16's run is the only second sample of the
  baseline, and it comes from another session two days earlier.
- **"The same model" was not one build.** Every call recorded the build that answered it: the
  baseline trial was served by 6 builds and the shorter-thoughts trial by 7, and 33 of the 36 jobs
  were answered by more than one build across their own calls, up to 6 in one job. Temperature 0 is
  not determinism here: the unchanged prompt matched M16's run token for token on only 7 of 18
  tasks.
- **Expectations met can't go up from 100%.** Both arms met every expectation, so this experiment
  can only show the sentence didn't break anything the hidden tests check. Shorter thoughts might
  cost something the tests don't see, such as how well a final answer explains itself.
- **The price is unverified against its page.** It came from a web search's summary of Groq's model
  page, which this machine's network policy blocks; the key is on the free tier, which charges
  nothing. Every cost here is at that list price, and `bench/prices.json` says so.
- **The interrupted trials say less than they could.** Their records say only that the daily cap was
  reached; the limit was read from the provider afterwards. Records made from now on keep the
  provider's own message (`ea58397`).
- **Records got bigger.** An 18-job trial record with outputs and diffs is 56–83 KB; the whole
  experiment, recordings included, is 716 KB.

## Reproduce

```bash
docs/results/bench/m22/run.sh round 1        # each arm once, on the real model (GROQ_API_KEY)
docs/results/bench/m22/run.sh round 2
docs/results/bench/m22/run.sh round 3
docs/results/bench/m22/run.sh compare        # the comparison above
docs/results/bench/m22/run.sh replays        # each arm's first trial, replayed from its records
python3 docs/results/bench/m22/analysis.py   # every number in this document
```
