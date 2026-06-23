#!/usr/bin/env bash
# scripts/start.sh
# Generates SSH keys (if needed) and starts the MCP server.
set -euo pipefail

BASE_DIR="${FC_BASE_DIR:-/opt/fc-mcp}"
SSH_KEY="$BASE_DIR/vm_ssh_key"
MCP_PORT="${MCP_PORT:-8080}"
MCP_HOST="${MCP_HOST:-0.0.0.0}"

mkdir -p "$BASE_DIR"/{vm-images,snapshots,sockets,overlays}

# ── SSH key for VM access ──────────────────────────────────────────────────────
if [[ ! -f "$SSH_KEY" ]]; then
    echo "==> Generating SSH key for VM access..."
    ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "fc-mcp-vm-access"
    echo "✅ SSH key generated at $SSH_KEY"
fi
# Keep <key>.pub consistent with the (possibly mounted, read-only) private key.
# `ssh -i <key>` reads <key>.pub to decide which key to OFFER; a stale .pub left
# in the data dir makes it offer the wrong key and auth fails even with the right
# private key. Always derive the .pub from the private key.
ssh-keygen -y -f "$SSH_KEY" > "$SSH_KEY.pub" 2>/dev/null || true

# ── Preflight checks ──────────────────────────────────────────────────────────
echo "==> Checking prerequisites..."

if [[ ! -e /dev/kvm ]]; then
    echo "❌ /dev/kvm not found. Firecracker requires KVM."
    echo "   On EC2: use a metal instance or instance with nested virt support"
    echo "   On bare metal: ensure VT-x/AMD-V is enabled in BIOS"
    echo "   On Proxmox/VMware/VirtualBox: enable nested virtualization on the hypervisor"
    echo ""
    echo "   Try: sudo modprobe kvm_intel  (or kvm_amd)"
    exit 1
fi

KERNEL="$BASE_DIR/vm-images/vmlinux-5.10"
ROOTFS="$BASE_DIR/vm-images/ubuntu-22.04-base.ext4"

if [[ ! -f "$KERNEL" ]]; then
    echo "❌ Kernel not found at $KERNEL"
    echo "   Run: scripts/build-kernel.sh"
    exit 1
fi

if [[ ! -f "$ROOTFS" ]]; then
    echo "❌ Rootfs not found at $ROOTFS"
    echo "   Run: sudo scripts/build-rootfs.sh"
    exit 1
fi

if ! command -v firecracker &>/dev/null && [[ ! -f /usr/bin/firecracker ]]; then
    echo "❌ firecracker binary not found"
    echo "   Install: https://github.com/firecracker-microvm/firecracker/releases"
    exit 1
fi

echo "✅ All checks passed"

# ── Start MCP server ──────────────────────────────────────────────────────────
echo ""
echo "==> Starting Firecracker Bash MCP Server"
echo "    Host: $MCP_HOST:$MCP_PORT"
echo "    Base dir: $BASE_DIR"
echo ""

export FC_BASE_DIR="$BASE_DIR"

# ── Egress broker (optional, standalone mode) ───────────────────────────────────
# In Kubernetes fc-egress runs as a sidecar container; for standalone/Docker we launch it
# here in the background. The nft TPROXY rules come from setup-network.sh (run with
# FC_EGRESS_ENABLED=1). The egress CA is generated on first run under $FC_EGRESS_CA_DIR.
if [ -n "${FC_EGRESS_ENABLED:-}" ] && [ "${FC_EGRESS_ENABLED}" != "0" ]; then
    EGRESS_CA_DIR="${FC_EGRESS_CA_DIR:-$BASE_DIR/egress-ca}"
    mkdir -p "$EGRESS_CA_DIR"
    if command -v fc-egress &>/dev/null; then
        fc-egress --generate-ca --ca-cert "$EGRESS_CA_DIR/ca.crt" --ca-key "$EGRESS_CA_DIR/ca.key" || true
        echo "==> Launching fc-egress broker (background)"
        fc-egress \
            --index "$BASE_DIR/egress-index.json" \
            --ca-cert "$EGRESS_CA_DIR/ca.crt" --ca-key "$EGRESS_CA_DIR/ca.key" \
            --github-backend "${FC_EGRESS_GITHUB_BACKEND:-octosts}" \
            --octosts-url "${FC_EGRESS_OCTOSTS_URL:-}" \
            --app-id "${FC_EGRESS_APP_ID:-0}" --app-key "${FC_EGRESS_APP_KEY:-}" &
    else
        echo "⚠ FC_EGRESS_ENABLED but fc-egress not on PATH; skipping broker launch"
    fi
fi

# Resolve the repo root that holds server.py + pyproject.toml. It's either this script's own
# dir (the Docker image copies everything flat into /app) or its parent (the repo, where this
# script lives under scripts/). Run uv from there so it finds pyproject.toml.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "$ROOT_DIR/server.py" ]] || ROOT_DIR="$(cd "$ROOT_DIR/.." && pwd)"
cd "$ROOT_DIR"
exec uv run python server.py --host "$MCP_HOST" --port "$MCP_PORT"
