#!/usr/bin/env bash
# scripts/build-rootfs.sh
# Builds a minimal Ubuntu 22.04 root filesystem image for Firecracker.
# Requires: debootstrap, e2fsprogs, qemu-user-static (for cross-arch)
# Run as root on a Linux host.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"   # scripts/ lives under the repo root
BASE_DIR="${FC_BASE_DIR:-/opt/fc-mcp}"
IMAGES_DIR="$BASE_DIR/vm-images"
ROOTFS="$IMAGES_DIR/ubuntu-22.04-base.ext4"
ROOTFS_SIZE_MB="${ROOTFS_SIZE_MB:-2048}"
MOUNT_DIR=$(mktemp -d)

echo "==> Building Firecracker rootfs at $ROOTFS ($ROOTFS_SIZE_MB MB)"
mkdir -p "$IMAGES_DIR"

# ── 1. Create sparse ext4 image ────────────────────────────────────────────────
echo "==> Creating ${ROOTFS_SIZE_MB}MB ext4 image..."
dd if=/dev/zero of="$ROOTFS" bs=1M count="$ROOTFS_SIZE_MB" status=progress
mkfs.ext4 -F "$ROOTFS"

# ── 2. Bootstrap Ubuntu into the image ────────────────────────────────────────
echo "==> Mounting image..."
mount -o loop "$ROOTFS" "$MOUNT_DIR"

echo "==> Running debootstrap (Ubuntu 22.04 jammy)..."
debootstrap --include=curl,wget,vim,git,htop,net-tools,iproute2,iputils-ping,sudo,bash,systemd,tmux \
    jammy "$MOUNT_DIR" http://archive.ubuntu.com/ubuntu/

# ── 3. Configure the system ───────────────────────────────────────────────────
echo "==> Configuring system..."

# Set root password (you should change this or use SSH keys only)
echo "root:firecracker" | chroot "$MOUNT_DIR" chpasswd

# Set hostname
echo "fc-vm" > "$MOUNT_DIR/etc/hostname"

# Network config (eth0 with static IP set by kernel cmdline)
mkdir -p "$MOUNT_DIR/etc/network"
cat > "$MOUNT_DIR/etc/network/interfaces" <<'EOF'
auto lo
iface lo inet loopback

auto eth0
iface eth0 inet dhcp
EOF

# No sshd: command exec goes through fc-agent (installed below). Serial getty kept for debug.

# ── fc-agent guest agent (HTTP command exec; replaces SSH) ────────────────────
# The host drives commands into the VM via this agent over the tap instead of SSH.
# Build it first with: bash scripts/build-agent.sh (produces the per-arch binaries in bin/).
_fc_arch="$(case "$(uname -m)" in aarch64|arm64) echo arm64;; *) echo amd64;; esac)"
FC_AGENT_BIN="${FC_AGENT_BIN:-$REPO_DIR/bin/fc-agent-$_fc_arch}"
if [[ -f "$FC_AGENT_BIN" ]]; then
    echo "==> Installing fc-agent guest agent..."
    install -D -m 0755 "$FC_AGENT_BIN" "$MOUNT_DIR/usr/local/bin/fc-agent"
    mkdir -p "$MOUNT_DIR/etc/fc-agent"   # per-VM token is written here into the overlay at VM-create time
    cat > "$MOUNT_DIR/etc/systemd/system/fc-agent.service" <<'EOF'
[Unit]
Description=fc-mcp guest command agent
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=/usr/local/bin/fc-agent --listen-http 2025 --listen-ws 2024 --allow-from 172.16.0.1 --token-file /etc/fc-agent/token
Restart=always
RestartSec=1
[Install]
WantedBy=multi-user.target
EOF
    mkdir -p "$MOUNT_DIR/etc/systemd/system/multi-user.target.wants"
    chroot "$MOUNT_DIR" systemctl enable fc-agent.service 2>/dev/null || \
        ln -sf /etc/systemd/system/fc-agent.service \
            "$MOUNT_DIR/etc/systemd/system/multi-user.target.wants/fc-agent.service"
else
    echo "WARNING: fc-agent not found at $FC_AGENT_BIN — run 'bash scripts/build-agent.sh' first; rootfs will lack the agent."
fi

# Disable unnecessary services for faster boot
chroot "$MOUNT_DIR" systemctl disable apt-daily.service apt-daily-upgrade.service \
    unattended-upgrades.service systemd-timesyncd.service 2>/dev/null || true

# Fast serial console getty
mkdir -p "$MOUNT_DIR/etc/systemd/system/serial-getty@ttyS0.service.d"
cat > "$MOUNT_DIR/etc/systemd/system/serial-getty@ttyS0.service.d/override.conf" <<'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear %I 115200 linux
EOF

# Fstab
echo "LABEL=rootfs / ext4 defaults,errors=remount-ro 0 1" > "$MOUNT_DIR/etc/fstab"

# ── 4. Tune ext4 label ────────────────────────────────────────────────────────
umount "$MOUNT_DIR"
e2label "$ROOTFS" rootfs
rmdir "$MOUNT_DIR"

echo ""
echo "✅ Rootfs built at: $ROOTFS"
echo ""
echo "Next: run scripts/build-kernel.sh to get the kernel image"
