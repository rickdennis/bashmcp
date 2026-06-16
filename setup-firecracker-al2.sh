#!/usr/bin/env bash
# setup-firecracker-al2.sh
# One-shot host setup for bashmcp on Amazon Linux 2.
# Differs from setup-firecracker.sh: yum (not apt), Docker-built rootfs (no
# debootstrap on AL2), uv installed system-wide so root/sudo can run it, and the
# SSH key generated BEFORE the rootfs build so its pubkey is baked into the image.
#
# Run as root:  sudo FC_BASE_DIR=/opt/fc-mcp bash setup-firecracker-al2.sh
set -euo pipefail

FC_VERSION="${FC_VERSION:-v1.10.1}"
FC_BASE_DIR="${FC_BASE_DIR:-/opt/fc-mcp}"
ARCH=$(uname -m)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# AL2 sudo secure_path is /sbin:/bin:/usr/sbin:/usr/bin and excludes /usr/local/bin
# (where uv installs), so add it or step 6's `uv sync` can't find uv.
export PATH="/usr/local/bin:$PATH"

log()  { echo "==> $*"; }
ok()   { echo "    ✓ $*"; }
skip() { echo "    (skip) $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

[[ "$(uname -s)" == "Linux" ]] || die "Firecracker requires Linux."
[[ "$(id -u)" -eq 0 ]]        || die "Run as root: sudo bash setup-firecracker-al2.sh"
[[ -e /dev/kvm ]]             || die "/dev/kvm not found."

log "Amazon Linux 2 setup  (FC_VERSION=$FC_VERSION  ARCH=$ARCH  FC_BASE_DIR=$FC_BASE_DIR)"

# 1. System deps (no debootstrap — rootfs is built via Docker)
log "[1/7] Installing system dependencies (yum)"
yum install -y openssh-clients e2fsprogs iproute iptables curl wget tar >/dev/null
ok "deps installed"

# 2. Docker (for the rootfs build)
log "[2/7] Installing + starting Docker"
command -v docker &>/dev/null || amazon-linux-extras install -y docker >/dev/null
systemctl enable --now docker
ok "docker running ($(docker --version 2>/dev/null))"

# 3. uv (system-wide so sudo/root finds it without PATH games)
log "[3/7] Installing uv"
if command -v uv &>/dev/null; then
    skip "uv present ($(uv --version))"
else
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh
    ok "uv installed to /usr/local/bin"
fi

# 4. Firecracker binary
log "[4/7] Installing Firecracker $FC_VERSION"
if command -v firecracker &>/dev/null && firecracker --version 2>/dev/null | grep -q "${FC_VERSION#v}"; then
    skip "firecracker present"
else
    TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
    curl -fsSL "https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-${ARCH}.tgz" | tar -xz -C "$TMP"
    install -m0755 "$TMP/release-${FC_VERSION}-${ARCH}/firecracker-${FC_VERSION}-${ARCH}" /usr/bin/firecracker
    ok "firecracker installed"
fi

# 5. VM SSH key — BEFORE the rootfs build so the pubkey is baked in
log "[5/7] Generating VM SSH key"
mkdir -p "$FC_BASE_DIR/vm-images"
if [[ -f "$FC_BASE_DIR/vm_ssh_key" ]]; then
    skip "key exists"
else
    ssh-keygen -t ed25519 -f "$FC_BASE_DIR/vm_ssh_key" -N "" -C fc-mcp-vm-access >/dev/null
    ok "key generated"
fi

# 6. Python deps + kernel + rootfs
log "[6/7] uv sync + kernel + rootfs"
(cd "$SCRIPT_DIR" && uv sync)
KERNEL="$FC_BASE_DIR/vm-images/vmlinux-5.10"
ROOTFS="$FC_BASE_DIR/vm-images/ubuntu-22.04-base.ext4"
if [[ -f "$KERNEL" ]]; then skip "kernel exists"; else FC_BASE_DIR="$FC_BASE_DIR" bash "$SCRIPT_DIR/build-kernel.sh"; fi
if [[ -f "$ROOTFS" ]]; then skip "rootfs exists"; else FC_BASE_DIR="$FC_BASE_DIR" bash "$SCRIPT_DIR/build-rootfs-docker.sh"; fi
ok "images ready"

# 7. Host networking
log "[7/7] Host networking (bridge + NAT)"
bash "$SCRIPT_DIR/setup-network.sh"
ok "network configured"

echo ""
echo "Setup complete. Start the node-agent:"
echo "  sudo FC_BASE_DIR=$FC_BASE_DIR bash start.sh"
