#!/bin/bash
# m21: the fixed task set, 72 jobs a batch, on pools of fleet workers of several sizes; within each
# run every pool's trials are interleaved with the others', their order reversed every other trial.
# run in this order, one after the other: idle, zero, past-cores, recorded, containers. past-cores
# goes beyond the planned worker counts, added after a first zero run (in ../m21-before-ready) to see
# the cpu ceiling rather than predict it
set -u
REPO=/home/user/agentic-coding-sandbox
OUT=$REPO/docs/results/bench/m21
LOGS=/tmp/claude-0/-home-user-agentic-coding-sandbox/cf8e8522-94c6-5425-95dc-0da5db2381a3/scratchpad/m21
PY=$REPO/backend/.venv/bin/python

scale() {
  local name=$1; shift
  $PY -m bench.cli scale --tasks 72 --out-root $OUT/$name "$@" > $LOGS/$name.log 2>&1
  echo "$name: exit $? | $(tail -1 $LOGS/$name.log)"
}

# all of it in a function bash reads whole before running, so an edit to this file mid-run changes
# nothing that is already running
main() {
  mkdir -p $LOGS
  cd $REPO/backend
  case $1 in
    idle)
      # what the host's cpus do for ten seconds with nothing of the bench running
      $PY - > $OUT/idle.json <<'EOF'
import asyncio, json
from bench.resources import Sampler, named

async def main():
    async with Sampler({"docker": named(["dockerd", "containerd"])}) as sampler:
        await asyncio.sleep(10)
    record = {"captured": "the host idle, just before the m21 runs"}
    print(json.dumps(record | sampler.result.model_dump(mode="json"), indent=2))

asyncio.run(main())
EOF
      ;;
    zero) scale zero --latency zero --workers 1 2 4 --trials 5 ;;
    past-cores) scale past-cores --latency zero --workers 4 8 --trials 5 ;;
    recorded) scale recorded --latency recorded --workers 1 2 4 8 16 --trials 5 ;;
    containers)
      # the docker daemon here does not always outlive a pause, so it is started again if it is gone
      docker info > /dev/null 2>&1 || { (dockerd > $LOGS/dockerd.log 2>&1 &); sleep 5; }
      docker info --format '{{json .}}' | $PY -c '
import json, sys
info = json.load(sys.stdin)
keys = ["ServerVersion", "DefaultRuntime", "Driver", "CgroupDriver", "CgroupVersion",
        "SecurityOptions", "KernelVersion", "OperatingSystem", "Architecture", "NCPU", "MemTotal"]
record = {"captured": "docker info, just before the m21 container run"}
print(json.dumps(record | {key: info.get(key) for key in keys}, indent=2))
' > $OUT/docker.json
      # 8 and 16 go beyond the planned 1, 2 and 4, to reach the point where containers stop scaling
      scale containers --latency recorded --execution container --workers 1 2 4 8 16 --trials 3
      ;;
  esac
}

main "$@"; exit $?
