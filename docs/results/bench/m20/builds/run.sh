#!/bin/bash
# the one-worker sequential against fleet a/b, run by the m19 and the m20 code alternately (ABBA), on one interpreter
set -u
REPO=/home/user/agentic-coding-sandbox
S=/tmp/claude-0/-home-user-agentic-coding-sandbox/cf8e8522-94c6-5425-95dc-0da5db2381a3/scratchpad/m20
OUT=/tmp/claude-0/-home-user-agentic-coding-sandbox/cf8e8522-94c6-5425-95dc-0da5db2381a3/scratchpad/m20/records/bench-m20-builds
PY=$REPO/backend/.venv/bin/python
W19=$S/m19-build
rm -rf $W19 $OUT
git -C $REPO worktree add -q --detach $W19 62ba4c5
# both trees start with their code compiled, so neither build pays for compiling it in a trial
(cd $W19/backend && $PY -m compileall -q app bench fleet)
(cd $REPO/backend && $PY -m compileall -q app bench fleet chaos)
order=(m19 m20 m20 m19 m19 m20 m20 m19 m19 m20)
for n in $(seq 1 10); do
  build=${order[$((n-1))]}
  if [ "$build" = m19 ]; then root=$W19/backend; else root=$REPO/backend; fi
  (cd $root && PYTHONPATH=. $PY -m bench.cli ab --trials 2 --out-root $OUT/$build/run-$n) > $S/builds-run-$n.log 2>&1
  echo "run $n ($build): exit $? | $(grep -E 'batch wall clock' $S/builds-run-$n.log | tr -s ' ' | tr '\n' ' ')"
done
git -C $REPO worktree remove --force $W19
