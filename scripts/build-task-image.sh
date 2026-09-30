#!/usr/bin/env bash
# Build the image fleet tasks run in. On a host that re-signs TLS, set CA_BUNDLE to its CA file.
set -euo pipefail
cd "$(dirname "$0")/.."
secret=()
if [ -n "${CA_BUNDLE:-}" ]; then
  secret=(--secret "id=ca,src=${CA_BUNDLE}")
fi
docker build -f backend/Dockerfile.task -t "${FLEET_TASK_IMAGE:-fleet-task:local}" "${secret[@]}" .
