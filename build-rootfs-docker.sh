#!/usr/bin/env bash
# build-rootfs-docker.sh
# Builds the Firecracker Ubuntu 22.04 rootfs using Docker instead of debootstrap.
# Portable: works on Amazon Linux 2 (no debootstrap available) or any Docker host,
# and is arch-native (the image matches the host's architecture).
#
# Run as root. Requires: docker, e2fsprogs (mkfs.ext4), and $FC_BASE_DIR/vm_ssh_key.pub
# (generate the keypair FIRST so the public key is baked into the image).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="${FC_BASE_DIR:-/opt/fc-mcp}"
IMAGES_DIR="$BASE_DIR/vm-images"
ROOTFS="$IMAGES_DIR/ubuntu-22.04-base.ext4"
ROOTFS_SIZE_MB="${ROOTFS_SIZE_MB:-2048}"
PUBKEY="$BASE_DIR/vm_ssh_key.pub"
_fc_arch="$(case "$(uname -m)" in aarch64|arm64) echo arm64;; *) echo amd64;; esac)"
FC_AGENT_BIN="${FC_AGENT_BIN:-$SCRIPT_DIR/bin/fc-agent-$_fc_arch}"   # built by build-agent.sh

command -v docker >/dev/null || { echo "ERROR: docker is required"; exit 1; }
[[ -f "$PUBKEY" ]] || {
    echo "ERROR: $PUBKEY missing. Generate the keypair first:"
    echo "  ssh-keygen -t ed25519 -f $BASE_DIR/vm_ssh_key -N ''"
    exit 1
}

mkdir -p "$IMAGES_DIR"
BUILD=$(mktemp -d)
MNT=$(mktemp -d)
cleanup() { mountpoint -q "$MNT" && umount "$MNT"; rm -rf "$BUILD" "$MNT"; }
trap cleanup EXIT

cp "$PUBKEY" "$BUILD/authorized_keys"

cat > "$BUILD/Dockerfile" <<'DOCKER'
FROM ubuntu:22.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      systemd systemd-sysv openssh-server sudo bash tmux \
      curl wget vim git htop net-tools iproute2 iputils-ping ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# sshd: root login, pubkey only
RUN sed -i 's/#\?PermitRootLogin.*/PermitRootLogin yes/'            /etc/ssh/sshd_config \
 && sed -i 's/#\?PubkeyAuthentication.*/PubkeyAuthentication yes/'  /etc/ssh/sshd_config \
 && sed -i 's/#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config \
 && ssh-keygen -A \
 && ln -sf /lib/systemd/system/ssh.service /etc/systemd/system/multi-user.target.wants/ssh.service \
 && mkdir -p /etc/systemd/system/serial-getty@ttyS0.service.d \
 && printf '[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin root --noclear %%I 115200 linux\n' \
      > /etc/systemd/system/serial-getty@ttyS0.service.d/override.conf \
 && echo fc-vm > /etc/hostname \
 && echo 'LABEL=rootfs / ext4 defaults,errors=remount-ro 0 1' > /etc/fstab \
 && mkdir -p /root/.ssh && chmod 700 /root/.ssh
COPY authorized_keys /root/.ssh/authorized_keys
RUN chmod 600 /root/.ssh/authorized_keys
DOCKER

# Bake in the fc-agent guest agent (HTTP command exec; replaces SSH). Build it first
# with: bash build-agent.sh. sshd stays as a debug fallback until a later phase.
if [[ -f "$FC_AGENT_BIN" ]]; then
    echo "==> including fc-agent in the image"
    cp "$FC_AGENT_BIN" "$BUILD/fc-agent"
    cat >> "$BUILD/Dockerfile" <<'DOCKER2'
COPY fc-agent /usr/local/bin/fc-agent
RUN chmod 0755 /usr/local/bin/fc-agent \
 && mkdir -p /etc/fc-agent \
 && printf '[Unit]\nDescription=fc-mcp guest command agent\nAfter=network-online.target\nWants=network-online.target\n[Service]\nExecStart=/usr/local/bin/fc-agent --listen-http 2025 --listen-ws 2024 --allow-from 172.16.0.1 --token-file /etc/fc-agent/token\nRestart=always\nRestartSec=1\n[Install]\nWantedBy=multi-user.target\n' > /etc/systemd/system/fc-agent.service \
 && ln -sf /etc/systemd/system/fc-agent.service /etc/systemd/system/multi-user.target.wants/fc-agent.service
DOCKER2
else
    echo "WARNING: fc-agent not found at $FC_AGENT_BIN — run 'bash build-agent.sh' first; rootfs will lack the agent."
fi

IMG="fc-rootfs-build:tmp"
echo "==> docker build rootfs image (arch: $(uname -m))"
docker build -t "$IMG" "$BUILD"
CID=$(docker create "$IMG")

echo "==> creating ${ROOTFS_SIZE_MB}MB ext4 at $ROOTFS"
dd if=/dev/zero of="$ROOTFS" bs=1M count="$ROOTFS_SIZE_MB" status=none
mkfs.ext4 -F -L rootfs "$ROOTFS" >/dev/null
mount -o loop "$ROOTFS" "$MNT"

echo "==> exporting container filesystem into the image"
docker export "$CID" | tar -x -C "$MNT"
docker rm "$CID" >/dev/null
docker rmi "$IMG" >/dev/null 2>&1 || true

# Config that can't stick during a docker build (resolv.conf is bind-mounted then;
# machine-id must be empty so systemd regenerates it on first boot).
printf 'nameserver 8.8.8.8\n' > "$MNT/etc/resolv.conf"
: > "$MNT/etc/machine-id"
sync
umount "$MNT"

echo "✅ Rootfs built at $ROOTFS"
