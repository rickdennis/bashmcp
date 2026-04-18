#!/usr/bin/env bash
# scripts/setup-network.sh
# Sets up host networking for Firecracker VMs:
#   - Creates a bridge (fc-br0) for VM communication
#   - Configures NAT/masquerade so VMs can reach the internet
#   - Sets up tap device management helpers
#
# Run once at host boot (or add to systemd service).
# Requires: iproute2, iptables, bridge-utils
set -euo pipefail

BRIDGE="fc-br0"
BRIDGE_IP="172.16.0.1/24"
HOST_IFACE="${HOST_IFACE:-$(ip route show default | awk '/default/ {print $5}' | head -1)}"

echo "==> Setting up Firecracker network bridge"
echo "    Bridge: $BRIDGE ($BRIDGE_IP)"
echo "    Host interface: $HOST_IFACE"

# ── Bridge ─────────────────────────────────────────────────────────────────────
if ! ip link show "$BRIDGE" &>/dev/null; then
    ip link add name "$BRIDGE" type bridge
fi
ip addr flush dev "$BRIDGE" 2>/dev/null || true
ip addr add "$BRIDGE_IP" dev "$BRIDGE"
ip link set "$BRIDGE" up

# ── IP forwarding ──────────────────────────────────────────────────────────────
echo 1 > /proc/sys/net/ipv4/ip_forward
# Persist across reboots
grep -qxF 'net.ipv4.ip_forward=1' /etc/sysctl.conf || echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf

# ── NAT (masquerade) ───────────────────────────────────────────────────────────
# Allow VMs to reach the internet via host
iptables -t nat -C POSTROUTING -s 172.16.0.0/24 -o "$HOST_IFACE" -j MASQUERADE 2>/dev/null || \
    iptables -t nat -A POSTROUTING -s 172.16.0.0/24 -o "$HOST_IFACE" -j MASQUERADE

# Allow bridge-host traffic
iptables -C FORWARD -i "$BRIDGE" -j ACCEPT 2>/dev/null || \
    iptables -A FORWARD -i "$BRIDGE" -j ACCEPT
iptables -C FORWARD -o "$BRIDGE" -j ACCEPT 2>/dev/null || \
    iptables -A FORWARD -o "$BRIDGE" -j ACCEPT

# ── SSH port forwarding for each VM ───────────────────────────────────────────
# VMs bind SSH on 172.16.0.X:22 inside the VM
# Host forwards 50000+N on localhost to each VM
# This is done per-VM by the MCP server's vm_create; this script just
# ensures the infrastructure is in place.

echo ""
echo "✅ Network setup complete"
echo "   Bridge: $BRIDGE @ 172.16.0.1"
echo "   VMs will get IPs 172.16.0.2+"
echo "   SSH to VM 0: ssh -p 50000 root@localhost"
echo ""
echo "   To add SSH forwarding for a VM manually:"
echo "   iptables -t nat -A PREROUTING -p tcp --dport 50000 -j DNAT --to 172.16.0.2:22"
echo "   iptables -t nat -A PREROUTING -p tcp --dport 50001 -j DNAT --to 172.16.0.3:22"

# ── Helper: create tap device for a VM ────────────────────────────────────────
create_tap() {
    local tap_name="$1"
    if ! ip link show "$tap_name" &>/dev/null; then
        ip tuntap add dev "$tap_name" mode tap
        ip link set dev "$tap_name" master "$BRIDGE"
        ip link set dev "$tap_name" up
        echo "Created tap: $tap_name"
    fi
}

# Pre-create tap devices for up to 32 VMs
for i in $(seq 0 31); do
    create_tap "fc-tap-$(printf '%08x' $i)"
done

echo "==> Pre-created 32 tap devices (fc-tap-*)"
