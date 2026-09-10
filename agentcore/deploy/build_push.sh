#!/usr/bin/env bash
# Build and push the bashmcp images. Default target is the firm's mgmt-account ECR
# (devops/bashmcp-*); the standalone path can point at another registry.
#
#   bash deploy/build_push.sh --tag 0.1.0                 # both images
#   bash deploy/build_push.sh --tag 0.1.0 --sandbox-only  # arm64 only (AgentCore requirement)
#   bash deploy/build_push.sh --tag 0.1.0 --broker-only   # linux/amd64 + linux/arm64 for EKS
#
# The tag you push is what goes into devops-live bashmcp.auto.tfvars.json (sandbox;
# bumping it WIPES every session's /mnt/workspace) and k8s-devops apps/bashmcp (broker).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
REGISTRY="${ECR_REGISTRY:-400566821654.dkr.ecr.us-east-1.amazonaws.com}"
SANDBOX_REPO="${SANDBOX_REPO:-devops/bashmcp-sandbox}"
BROKER_REPO="${BROKER_REPO:-devops/bashmcp-broker}"
REGION="us-east-1"
PROFILE="${AWS_PROFILE:-}"
TAG="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || date -u +%Y%m%d%H%M%S)"
BUILD_SANDBOX=1
BUILD_BROKER=1
BROKER_PLATFORMS="linux/amd64,linux/arm64"
ARTIFACTORY_HOST="artifactory.stoneridgeam.com"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --registry) REGISTRY="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    --sandbox-only) BUILD_BROKER=0; shift ;;
    --broker-only) BUILD_SANDBOX=0; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
PROFILE_ARGS=(); [[ -n "$PROFILE" ]] && PROFILE_ARGS=(--profile "$PROFILE")

echo "== docker login (Artifactory anonymous pull; ECR push to ${REGISTRY}) =="
echo anonymous | docker login "$ARTIFACTORY_HOST" --username anonymous --password-stdin >/dev/null
aws ecr get-login-password "${PROFILE_ARGS[@]}" --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null

if [[ "$BUILD_SANDBOX" == 1 ]]; then
  echo "== build+push ${REGISTRY}/${SANDBOX_REPO}:${TAG} (linux/arm64) =="
  docker buildx build --platform linux/arm64 --provenance=false --sbom=false \
    -t "${REGISTRY}/${SANDBOX_REPO}:${TAG}" --push "$ROOT/sandbox"
fi

if [[ "$BUILD_BROKER" == 1 ]]; then
  echo "== build+push ${REGISTRY}/${BROKER_REPO}:${TAG} (${BROKER_PLATFORMS}) =="
  docker buildx build --platform "$BROKER_PLATFORMS" --provenance=false --sbom=false \
    -f "$ROOT/broker/Dockerfile" -t "${REGISTRY}/${BROKER_REPO}:${TAG}" --push "$ROOT"
fi

if [[ -f "$HERE/outputs.json" ]]; then
  (cd "$ROOT" && [[ "$BUILD_SANDBOX" == 1 ]] && uv run python deploy/_common.py set images.sandbox "${REGISTRY}/${SANDBOX_REPO}:${TAG}" || true)
  (cd "$ROOT" && [[ "$BUILD_BROKER" == 1 ]] && uv run python deploy/_common.py set images.broker "${REGISTRY}/${BROKER_REPO}:${TAG}" || true)
fi
echo "== done: tag ${TAG} =="
