#!/usr/bin/env bash
# Build the image fleet tasks run in. On a host that re-signs TLS, set CA_BUNDLE to its CA file.
set -euo pipefail
cd "$(dirname "$0")/.."
secret=()
if [ -n "${CA_BUNDLE:-}" ]; then
  secret=(--secret "id=ca,src=${CA_BUNDLE}")
fi
# a hash of what goes into the image, so a stale image can be told apart from a fresh one
source_hash=$(cd backend && python3 -c "from fleet.source import source_hash; print(source_hash())")
docker build -f backend/Dockerfile.task -t "${FLEET_TASK_IMAGE:-fleet-task:local}" \
  --label "fleet.source=${source_hash}" "${secret[@]}" .
