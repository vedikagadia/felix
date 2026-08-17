#!/usr/bin/env bash
# Build the one felix image and push it to ECR. All three roles (web+watch
# merged, sample) run from this single image — the task defs pick the role via
# the ROLE env var. See DEPLOY.md §5.1.
#
#   ./deploy/build-and-push.sh              # build + push :latest
#   TAG=v2 ./deploy/build-and-push.sh       # build + push :v2 (and :latest)
#
# Prereqs: docker running, AWS CLI configured as felix-deployer, the ECR repo
# `felix` created (DEPLOY.md §3.1). Nothing here is secret — the image carries
# no credentials; DATABASE_URL / GEMINI_API_KEY / DUCKDNS_TOKEN are injected at
# runtime via the task def, never baked in.
set -euo pipefail

ACCOUNT_ID="${ACCOUNT_ID:-287211515912}"
REGION="${AWS_REGION:-us-east-1}"
REPO="${REPO:-felix}"
TAG="${TAG:-latest}"

REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
IMAGE="${REGISTRY}/${REPO}"

# Repo root = one dir up from this script, so the build context is correct
# regardless of where the script is invoked from.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> building ${IMAGE}:${TAG} (linux/amd64 — Fargate is x86)"
# --platform pins amd64: a build on an Apple-silicon Mac defaults to arm64,
# which Fargate's default x86 tasks can't run.
docker build --platform linux/amd64 -t "${IMAGE}:${TAG}" "${ROOT}"

echo "==> logging in to ECR"
aws ecr get-login-password --region "${REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}"

echo "==> pushing ${IMAGE}:${TAG}"
docker push "${IMAGE}:${TAG}"

# Keep a moving :latest alongside an explicit tag when TAG != latest, so the
# task defs (which reference :latest) always pull the newest push.
if [ "${TAG}" != "latest" ]; then
  docker tag "${IMAGE}:${TAG}" "${IMAGE}:latest"
  docker push "${IMAGE}:latest"
fi

echo "==> done: ${IMAGE}:${TAG}"
