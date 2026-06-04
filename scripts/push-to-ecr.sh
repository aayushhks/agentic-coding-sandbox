#!/usr/bin/env bash
# Build the self-contained image and push it to Amazon ECR, ready for App Runner / ECS / EC2.
#
# Usage: scripts/push-to-ecr.sh [region] [repo-name]
#   region defaults to $AWS_REGION or us-east-1; repo-name defaults to agentic-coding-sandbox.
# Requires the AWS CLI (authenticated) and Docker.
set -euo pipefail

REGION="${1:-${AWS_REGION:-us-east-1}}"
REPO="${2:-agentic-coding-sandbox}"

# run from the repo root so the Docker build context is correct regardless of CWD
cd "$(dirname "$0")/.."

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
IMAGE="${REGISTRY}/${REPO}:latest"

echo "==> ensuring ECR repository '${REPO}' exists in ${REGION}"
aws ecr describe-repositories --repository-names "${REPO}" --region "${REGION}" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "${REPO}" --region "${REGION}" >/dev/null

echo "==> logging Docker in to ${REGISTRY}"
aws ecr get-login-password --region "${REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}"

echo "==> building and pushing ${IMAGE}"
docker build -t "${IMAGE}" .
docker push "${IMAGE}"

echo "==> done: ${IMAGE}"
echo "    Point App Runner at this image (port 8000, health check path /health)."
