#!/bin/bash
# the real agent on the real model through the whole platform: the 18-task set once, on 4 fleet
# workers, each job in its worker's own process, with the default execution policy, lease and agent
# config. it runs from a clean worktree of the commit, so edits elsewhere can't mark its records
# dirty, and waits until the provider's daily token budget has refilled enough for a whole batch
set -u
REPO=/home/user/agentic-coding-sandbox
SCRATCH=/tmp/claude-0/-home-user-agentic-coding-sandbox/cf8e8522-94c6-5425-95dc-0da5db2381a3/scratchpad
W=$SCRATCH/real-fleet-worktree
PY=$REPO/backend/.venv/bin/python
SHA=$1
START_AT=$2
LABEL=fleet-4w-real-qwen3.8-27b

probe() {
  $PY - <<'EOF'
import asyncio, os, sys
from groq import AsyncGroq, RateLimitError

async def main() -> int:
    client = AsyncGroq(api_key=os.environ["GROQ_API_KEY"], max_retries=0)
    try:
        await client.chat.completions.create(
            model="qwen/qwen3.8-27b",
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            temperature=0.0, max_tokens=4,
        )
    except RateLimitError as exc:
        print(f"probe refused: {exc}", flush=True)
        return 1
    return 0

sys.exit(asyncio.run(main()))
EOF
}

# all of it in a function bash reads whole before running, so an edit to this file mid-run changes
# nothing that is already running
main() {
  git -C $REPO worktree add -q --detach $W $SHA
  echo "worktree at $SHA, waiting until $(date -u -d @$START_AT +%H:%M) UTC"
  while [ "$(date +%s)" -lt "$START_AT" ]; do sleep 60; done
  until probe; do sleep 900; done
  echo "starting at $(date -u +%H:%M:%S) UTC"
  cd $W/backend
  PYTHONPATH=$W/backend $PY -m bench.cli record --executor fleet --workers 4 --trial 1 \
    --label $LABEL --out $W/docs/results/bench/real-fleet/$LABEL \
    --recordings-out $W/docs/results/bench/real-fleet/$LABEL/recordings/trial-1
  echo "record: exit $? at $(date -u +%H:%M:%S) UTC"
}

main "$@"; exit $?
