#!/bin/bash
# sequential replay from three fresh worktrees side by side: m19 code, m20 code, and m20 code with only the grading change undone
set -u
REPO=/home/user/agentic-coding-sandbox
S=/tmp/claude-0/-home-user-agentic-coding-sandbox/cf8e8522-94c6-5425-95dc-0da5db2381a3/scratchpad/m20
OUT=$S/records/bench-m20-three-arms
PY=$REPO/backend/.venv/bin/python
T=$S/arms
rm -rf $T $OUT; mkdir -p $T $OUT
git -C $REPO worktree add -q --detach $T/m19 62ba4c5
git -C $REPO worktree add -q --detach $T/m20 8117d39
git -C $REPO worktree add -q --detach $T/m20-grading-on-loop 8117d39
(cd $T/m20-grading-on-loop/backend && $PY $S/mutations/apply.py $S/grading-on-the-loop.json)
git -C $T/m20-grading-on-loop diff > $OUT/m20-grading-on-loop.diff
for arm in m19 m20 m20-grading-on-loop; do (cd $T/$arm/backend && $PY -m compileall -q app bench fleet); done
arms=(m19 m20 m20-grading-on-loop)
for round in $(seq 1 6); do
  # rotate the order each round so no arm always goes first
  for k in 0 1 2; do
    arm=${arms[$(( (round + k) % 3 ))]}
    (cd $T/$arm/backend && PYTHONPATH=. $PY -m bench.cli replay --trials 2 --label seq-$arm --out $OUT/$arm/round-$round) > $S/arms-$arm-$round.log 2>&1
    echo "round $round $arm: exit $? | $(grep -E 'batch wall clock' $S/arms-$arm-$round.log | tr -s ' ')"
  done
done
for arm in m19 m20 m20-grading-on-loop; do git -C $REPO worktree remove --force $T/$arm; done
echo "== done"
