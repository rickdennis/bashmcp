#!/usr/bin/env bash
# build-agent.sh — cross-compile the fc-agent guest binary (static, no CGO).
# Output: bin/fc-agent-{amd64,arm64} plus bin/fc-agent (host-arch convenience copy
# that build-rootfs.sh bakes into the guest image). Mirrors how fcctl is built.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$SCRIPT_DIR/bin"
mkdir -p "$OUT"
cd "$SCRIPT_DIR/agent"

echo "==> Building fc-agent (linux/amd64, linux/arm64)..."
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o "$OUT/fc-agent-amd64" .
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags="-s -w" -o "$OUT/fc-agent-arm64" .

# host-arch convenience copy (the rootfs build defaults to $SCRIPT_DIR/bin/fc-agent)
case "$(uname -m)" in
  x86_64)        cp "$OUT/fc-agent-amd64" "$OUT/fc-agent" ;;
  aarch64|arm64) cp "$OUT/fc-agent-arm64" "$OUT/fc-agent" ;;
esac

echo "✅ fc-agent built into $OUT/"
ls -lh "$OUT"/fc-agent* 2>/dev/null || true
