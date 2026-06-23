#!/usr/bin/env bash
# build-egress.sh — cross-compile the fc-egress broker binary (static, no CGO).
# Output: bin/fc-egress-{amd64,arm64} plus bin/fc-egress (host-arch convenience copy).
# The Dockerfile COPYs bin/fc-egress-amd64 into the node-agent image. Mirrors build-agent.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"   # scripts/ lives under the repo root
OUT="$REPO_DIR/bin"
mkdir -p "$OUT"
cd "$REPO_DIR/egress"

echo "==> Building fc-egress (linux/amd64, linux/arm64)..."
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o "$OUT/fc-egress-amd64" .
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags="-s -w" -o "$OUT/fc-egress-arm64" .

# host-arch convenience copy
case "$(uname -m)" in
  x86_64)        cp "$OUT/fc-egress-amd64" "$OUT/fc-egress" ;;
  aarch64|arm64) cp "$OUT/fc-egress-arm64" "$OUT/fc-egress" ;;
esac

echo "✅ fc-egress built into $OUT/"
ls -lh "$OUT"/fc-egress* 2>/dev/null || true
