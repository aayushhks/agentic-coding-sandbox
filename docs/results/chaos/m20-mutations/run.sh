#!/bin/bash
# every mutation on the final commit: one deliberate bug per throwaway worktree, then the scenarios that should catch it
set -u
REPO=/home/user/agentic-coding-sandbox
S=/tmp/claude-0/-home-user-agentic-coding-sandbox/cf8e8522-94c6-5425-95dc-0da5db2381a3/scratchpad/m20/mutations
OUT=/tmp/claude-0/-home-user-agentic-coding-sandbox/cf8e8522-94c6-5425-95dc-0da5db2381a3/scratchpad/m20/records/m20-mutations
PY=$REPO/backend/.venv/bin/python
mkdir -p $OUT
for spec in $S/*.json; do
  name=$(basename $spec .json)
  scenarios=$($PY -c "import json; print(json.load(open('$spec'))['scenarios'])")
  seeds=$($PY -c "import json; print(json.load(open('$spec'))['seeds'])")
  W=$S/worktrees/$name
  rm -rf $W $OUT/$name
  git -C $REPO worktree add -q --detach $W HEAD
  (cd $W/backend && $PY $S/apply.py $spec) || { echo "$name: did not apply"; git -C $REPO worktree remove --force $W; continue; }
  mkdir -p $OUT/$name
  git -C $W diff > $OUT/$name/mutation.diff
  $PY -c "import json; s = json.load(open('$spec')); print(json.dumps({'why': s['why'], 'scenarios': s['scenarios'], 'seeds': s['seeds']}, indent=2))" > $OUT/$name/mutation.json
  (cd $W/backend && PYTHONPATH=. $PY -m chaos run --scenarios $scenarios --seeds $seeds --out $OUT/$name) > $S/final-$name.log 2>&1
  echo "$name: exit $? | $(grep -E '^all ' $S/final-$name.log)"
  git -C $REPO worktree remove --force $W
done
