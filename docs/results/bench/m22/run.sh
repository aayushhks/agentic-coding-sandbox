#!/bin/bash
# m22: one sentence added to the agent's system prompt, against the prompt as it was, on the real
# model: three rounds of the 18-task set, each arm once a round, the order reversed every round;
# every trial keeps the model's responses, so it can be replayed exactly. run in this order:
# round 1, round 2, round 3, compare, replays
set -u
REPO=/home/user/agentic-coding-sandbox
OUT=$REPO/docs/results/bench/m22
LOGS=/tmp/claude-0/-home-user-agentic-coding-sandbox/cf8e8522-94c6-5425-95dc-0da5db2381a3/scratchpad/m22
PY=$REPO/backend/.venv/bin/python
RULE="Keep the thought field to one short sentence."

trial() {
  local arm=$1 round=$2
  local rule=()
  if [ $arm = short-thoughts ]; then rule=(--extra-rule "$RULE"); fi
  $PY -m bench.cli record --trial $round --label m22-$arm --out $OUT/$arm \
    --recordings-out $OUT/$arm/recordings/trial-$round "${rule[@]}" > $LOGS/$arm-$round.log 2>&1
  echo "round $round $arm: exit $? | $(tail -1 $LOGS/$arm-$round.log)"
}

# all of it in a function bash reads whole before running, so an edit to this file mid-run changes
# nothing that is already running
main() {
  mkdir -p $LOGS
  cd $REPO/backend
  case $1 in
    round)
      if [ $(($2 % 2)) -eq 1 ]; then order="baseline short-thoughts"; else order="short-thoughts baseline"; fi
      for arm in $order; do trial $arm $2; done
      ;;
    compare)
      $PY -m bench.cli compare --baseline $OUT/baseline --candidate $OUT/short-thoughts \
        --expect agent_configs --expect system_prompts --expect agent_digest \
        --out $OUT/comparison.json --markdown $OUT/comparison.md
      echo "compare: exit $?"
      ;;
    replays)
      # each arm's first trial replayed from its own responses, to show the record reproduces
      for arm in baseline short-thoughts; do
        local rule=()
        if [ $arm = short-thoughts ]; then rule=(--extra-rule "$RULE"); fi
        $PY -m bench.cli replay --trials 1 --label m22-$arm-replayed \
          --recordings $OUT/$arm/recordings/trial-1 --out $OUT/replayed/$arm "${rule[@]}" \
          > $LOGS/replay-$arm.log 2>&1
        echo "replay $arm: exit $? | $(tail -1 $LOGS/replay-$arm.log)"
      done
      ;;
  esac
}

main "$@"; exit $?
