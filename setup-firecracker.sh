#!/usr/bin/env bash
# setup-firecracker.sh
# One-shot host setup for bashmcp. Run once as root on a Linux+KVM host.
# Safe to re-run — each step checks before acting.
set -euo pipefail

FC_VERSION="${FC_VERSION:-v1.10.1}"
FC_BASE_DIR="${FC_BASE_DIR:-/opt/fc-mcp}"
ARCH=$(uname -m)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Helpers ───────────────────────────────────────────────────────────────────
log()  { echo "==> $*"; }
ok()   { echo "    ✓ $*"; }
skip() { echo "    (skip) $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

# ── Preflight ─────────────────────────────────────────────────────────────────
[[ "$(uname -s)" == "Linux" ]] || die "Firecracker requires Linux."
[[ "$(id -u)" -eq 0 ]]        || die "Run as root: sudo bash setup-firecracker.sh"

if [[ ! -e /dev/kvm ]]; then
    die "/dev/kvm not found. Enable KVM (Intel VT-x / AMD-V) or use a KVM-capable VM host."
fi

log "Starting Firecracker host setup"
echo "    FC_VERSION  : $FC_VERSION"
echo "    FC_BASE_DIR : $FC_BASE_DIR"
echo "    ARCH        : $ARCH"
echo ""

# ── Step 1: System dependencies ───────────────────────────────────────────────
log "[1/6] Installing system dependencies"
apt-get update -qq
apt-get install -y --no-install-recommends \
    wget curl ca-certificates \
    openssh-client \
    e2fsprogs \
    iproute2 iptables \
    debootstrap \
    qemu-user-static
ok "System dependencies installed"

# ── Step 2: uv ────────────────────────────────────────────────────────────────
log "[2/6] Installing uv (Python package manager)"
if command -v uv &>/dev/null; then
    skip "uv already installed ($(uv --version))"
else
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv installs to ~/.cargo/bin or ~/.local/bin; make it available immediately
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    ok "uv installed"
fi

# ── Step 3: Firecracker binary ────────────────────────────────────────────────
log "[3/6] Installing Firecracker $FC_VERSION"
if command -v firecracker &>/dev/null && firecracker --version 2>/dev/null | grep -q "${FC_VERSION#v}"; then
    skip "Firecracker $FC_VERSION already installed"
else
    TMP_DIR=$(mktemp -d)
    trap 'rm -rf "$TMP_DIR"' EXIT

    FC_URL="https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-${ARCH}.tgz"
    log "  Downloading $FC_URL"
    curl -fsSL "$FC_URL" | tar -xz -C "$TMP_DIR"

    install -m 0755 "$TMP_DIR/release-${FC_VERSION}-${ARCH}/firecracker-${FC_VERSION}-${ARCH}" /usr/bin/firecracker
    ok "Firecracker installed to /usr/bin/firecracker"
fi

# ── Step 4: Python dependencies ───────────────────────────────────────────────
log "[4/6] Installing Python dependencies (uv sync)"
(cd "$SCRIPT_DIR" && uv sync)
ok "Python dependencies ready"

# ── Step 5: Build VM images ───────────────────────────────────────────────────
IMAGES_DIR="$FC_BASE_DIR/vm-images"
KERNEL="$IMAGES_DIR/vmlinux-5.10"
ROOTFS="$IMAGES_DIR/ubuntu-22.04-base.ext4"

log "[5/6] Building VM images (FC_BASE_DIR=$FC_BASE_DIR)"
mkdir -p "$IMAGES_DIR"

if [[ -f "$KERNEL" ]]; then
    skip "Kernel already exists: $KERNEL"
else
    log "  Building kernel..."
    FC_BASE_DIR="$FC_BASE_DIR" bash "$SCRIPT_DIR/build-kernel.sh"
    ok "Kernel built"
fi

if [[ -f "$ROOTFS" ]]; then
    skip "Rootfs already exists: $ROOTFS"
else
    log "  Building rootfs (Ubuntu 22.04) — this takes ~5 minutes..."
    FC_BASE_DIR="$FC_BASE_DIR" bash "$SCRIPT_DIR/build-rootfs.sh"
    ok "Rootfs built"
fi

# ── Step 6: Host networking ───────────────────────────────────────────────────
log "[6/6] Configuring host networking (bridge + NAT)"
bash "$SCRIPT_DIR/setup-network.sh"
ok "Network configured"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Setup complete. Start the server with:"
echo ""
echo "    sudo FC_BASE_DIR=$FC_BASE_DIR bash start.sh"
echo ""
echo "  Then connect Claude Code:"
echo ""
echo "    claude mcp add --transport http firecracker-bash http://localhost:8080/mcp"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
