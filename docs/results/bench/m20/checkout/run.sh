#!/bin/bash
# the same code, sequential replay from the main checkout and from a fresh worktree, alternating
set -u
REPO=/home/user/agentic-coding-sandbox
S=/tmp/claude-0/-home-user-agentic-coding-sandbox/cf8e8522-94c6-5425-95dc-0da5db2381a3/scratchpad/m20
OUT=$S/records/bench-m20-checkout
PY=$REPO/backend/.venv/bin/python
W=$S/arms/worktree
rm -rf $W $OUT; mkdir -p $OUT
git -C $REPO worktree add -q --detach $W HEAD
(cd $W/backend && $PY -m compileall -q app bench fleet chaos)
(cd $REPO/backend && $PY -m compileall -q app bench fleet chaos)
for round in $(seq 1 6); do
  if [ $((round % 2)) -eq 1 ]; then order="main worktree"; else order="worktree main"; fi
  for arm in $order; do
    if [ $arm = main ]; then root=$REPO/backend; else root=$W/backend; fi
    (cd $root && PYTHONPATH=. $PY -m bench.cli replay --trials 2 --label seq-$arm --out $OUT/$arm/round-$round) > $S/checkout-$arm-$round.log 2>&1
    echo "round $round $arm: exit $? | $(grep -E 'batch wall clock' $S/checkout-$arm-$round.log | tr -s ' ')"
  done
done
git -C $REPO worktree remove --force $W
echo "== done"
