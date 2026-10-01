#!/bin/bash
# the grading fix as an a/b: head, against head with only that fix undone; agent scenarios, two cpus, seeds alternating
set -u
REPO=/home/user/agentic-coding-sandbox
S=/tmp/claude-0/-home-user-agentic-coding-sandbox/cf8e8522-94c6-5425-95dc-0da5db2381a3/scratchpad/m20
OUT=$REPO/docs/results/chaos/m20-agent-grading
PY=$REPO/backend/.venv/bin/python
W=$S/worktrees/grading-on-the-loop
rm -rf $W $OUT
mkdir -p $OUT/on-the-loop $OUT/off-the-loop
git -C $REPO worktree add -q --detach $W HEAD
(cd $W/backend && $PY $S/mutations/apply.py $S/grading-on-the-loop.json) || { echo "did not apply"; exit 1; }
git -C $W diff > $OUT/on-the-loop/mutation.diff
$PY -c "import json; s = json.load(open('$S/grading-on-the-loop.json')); print(json.dumps({'why': s['why']}, indent=2))" > $OUT/on-the-loop/mutation.json
SCEN=agent-heartbeat-before-kill,agent-publish-before-commit-kill
for seed in $(seq 0 9); do
  if [ $((seed % 2)) -eq 0 ]; then arms="on off"; else arms="off on"; fi
  for arm in $arms; do
    if [ $arm = on ]; then root=$W/backend; out=$OUT/on-the-loop; else root=$REPO/backend; out=$OUT/off-the-loop; fi
    (cd $root && PYTHONPATH=. taskset -c 0,1 $PY -m chaos run --scenarios $SCEN --seeds $seed --out $out) > $S/grading-$arm-$seed.log 2>&1
    echo "seed $seed $arm-the-loop: exit $? | $(grep -E '^all ' $S/grading-$arm-$seed.log)"
  done
done
rm -f $OUT/on-the-loop/summary.json $OUT/off-the-loop/summary.json
git -C $REPO worktree remove --force $W
