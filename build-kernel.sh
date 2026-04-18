#!/usr/bin/env bash
# scripts/build-kernel.sh
# Downloads a pre-built Firecracker-compatible kernel from AWS.
# Alternatively, builds from source if FC_BUILD_KERNEL=1 is set.
set -euo pipefail

BASE_DIR="${FC_BASE_DIR:-/opt/fc-mcp}"
IMAGES_DIR="$BASE_DIR/vm-images"
KERNEL="$IMAGES_DIR/vmlinux-5.10"

mkdir -p "$IMAGES_DIR"

if [[ "${FC_BUILD_KERNEL:-0}" == "1" ]]; then
    echo "==> Building kernel from source (this takes ~20 minutes)..."
    KERNEL_VERSION="5.10.225"
    KERNEL_SRC="/tmp/linux-${KERNEL_VERSION}"

    apt-get install -y build-essential flex bison libssl-dev libelf-dev bc

    wget -q "https://cdn.kernel.org/pub/linux/kernel/v5.x/linux-${KERNEL_VERSION}.tar.xz" -O /tmp/kernel.tar.xz
    tar -xf /tmp/kernel.tar.xz -C /tmp

    # Use Firecracker's recommended minimal config
    wget -q "https://raw.githubusercontent.com/firecracker-microvm/firecracker/main/resources/guest_configs/microvm-kernel-x86_64-5.10.config" \
        -O "$KERNEL_SRC/.config"

    cd "$KERNEL_SRC"
    make olddefconfig
    make vmlinux -j$(nproc)
    cp vmlinux "$KERNEL"
    cd -
else
    echo "==> Downloading pre-built Firecracker kernel (faster)..."

    # AWS provides pre-built kernels for Firecracker
    # This is the recommended 5.10 kernel from the Firecracker team
    KERNEL_URL="https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/x86_64/vmlinux-5.10.225"

    echo "Downloading from: $KERNEL_URL"
    wget -q --show-progress "$KERNEL_URL" -O "$KERNEL"
fi

chmod +x "$KERNEL"

echo ""
echo "✅ Kernel at: $KERNEL"
echo "   Size: $(du -sh "$KERNEL" | cut -f1)"
