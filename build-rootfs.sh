#!/usr/bin/env bash
# scripts/build-rootfs.sh
# Builds a minimal Ubuntu 22.04 root filesystem image for Firecracker.
# Requires: debootstrap, e2fsprogs, qemu-user-static (for cross-arch)
# Run as root on a Linux host.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
debootstrap --include=openssh-server,curl,wget,vim,git,htop,net-tools,iproute2,iputils-ping,sudo,bash,systemd \
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

# SSH config - allow root login with key
mkdir -p "$MOUNT_DIR/root/.ssh"
chmod 700 "$MOUNT_DIR/root/.ssh"

# If we have a public key, install it
if [[ -f "$BASE_DIR/vm_ssh_key.pub" ]]; then
    cp "$BASE_DIR/vm_ssh_key.pub" "$MOUNT_DIR/root/.ssh/authorized_keys"
    chmod 600 "$MOUNT_DIR/root/.ssh/authorized_keys"
fi

# Allow root SSH login
sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' "$MOUNT_DIR/etc/ssh/sshd_config"
sed -i 's/#PubkeyAuthentication.*/PubkeyAuthentication yes/' "$MOUNT_DIR/etc/ssh/sshd_config"
sed -i 's/PasswordAuthentication yes/PasswordAuthentication no/' "$MOUNT_DIR/etc/ssh/sshd_config"

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
