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
BRIDGE_IP="172.16.0.1/16"   # /16 supports up to 65 531 VMs; taps created on-demand by server.py
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
iptables -t nat -C POSTROUTING -s 172.16.0.0/16 -o "$HOST_IFACE" -j MASQUERADE 2>/dev/null || \
    iptables -t nat -A POSTROUTING -s 172.16.0.0/16 -o "$HOST_IFACE" -j MASQUERADE

# Allow bridge-host traffic
iptables -C FORWARD -i "$BRIDGE" -j ACCEPT 2>/dev/null || \
    iptables -A FORWARD -i "$BRIDGE" -j ACCEPT
iptables -C FORWARD -o "$BRIDGE" -j ACCEPT 2>/dev/null || \
    iptables -A FORWARD -o "$BRIDGE" -j ACCEPT

# ── Egress broker interception (fc-egress, optional) ───────────────────────────
# When FC_EGRESS_ENABLED is set, divert guest TLS (:443) to the local fc-egress proxy via
# an nftables TPROXY rule. The proxy's own re-originated traffic carries fwmark 0x1, which we
# skip so it isn't re-intercepted; the existing MASQUERADE still NATs that traffic out. This
# is additive (a dedicated `inet fcegress` table) and idempotent. Requires nftables.
if [ -n "${FC_EGRESS_ENABLED:-}" ] && [ "${FC_EGRESS_ENABLED}" != "0" ]; then
    EGRESS_ADDR="${FC_EGRESS_LISTEN:-127.0.0.1:3129}"
    EGRESS_IP="${EGRESS_ADDR%:*}"
    EGRESS_PORT="${EGRESS_ADDR##*:}"
    echo "==> Enabling fc-egress TPROXY interception of 172.16.0.0/16:443 -> ${EGRESS_ADDR}"
    # Policy routing: deliver TPROXY-marked packets to the local socket.
    ip rule list | grep -q "fwmark 0x1 lookup 100" || ip rule add fwmark 0x1 lookup 100
    ip route show table 100 2>/dev/null | grep -q "local default" || \
        ip route add local 0.0.0.0/0 dev lo table 100
    # Recreate the table so re-runs pick up config changes.
    nft list table inet fcegress >/dev/null 2>&1 && nft delete table inet fcegress
    # Use iptables REDIRECT (more compatible in kind/Docker than nftables TPROXY, which can
    # be bypassed by bridge traffic in nested-container environments). fc-egress recovers the
    # original destination via SO_ORIGINAL_DST. The fwmark OUTPUT rule prevents the re-originated
    # upstream traffic from being re-intercepted.
    # DNAT to 127.0.0.1 explicitly (not REDIRECT which rewrites to the incoming interface IP,
    # which would be 172.16.0.1 for bridge traffic — fc-egress listens on 127.0.0.1 only).
    # SO_ORIGINAL_DST on the accepted socket recovers the real upstream destination.
    iptables -t nat -C PREROUTING -s 172.16.0.0/16 -p tcp --dport 443 \
        -j DNAT --to-destination "127.0.0.1:${EGRESS_PORT}" 2>/dev/null || \
        iptables -t nat -A PREROUTING -s 172.16.0.0/16 -p tcp --dport 443 \
        -j DNAT --to-destination "127.0.0.1:${EGRESS_PORT}"
    echo "    iptables DNAT :443 -> 127.0.0.1:${EGRESS_PORT} installed"
fi

# ── SSH port forwarding for each VM ───────────────────────────────────────────
# VMs bind SSH on 172.16.0.X:22 inside the VM
# Host forwards 50000+N on localhost to each VM
# This is done per-VM by the MCP server's vm_create; this script just
# ensures the infrastructure is in place.

echo ""
echo "✅ Network setup complete"
echo "   Bridge: $BRIDGE @ 172.16.0.1/16"
echo "   VMs will get IPs 172.16.0.2+ (up to 172.16.255.254, ~65k slots)"
echo "   SSH to VM 0: ssh -p 50000 root@localhost"
echo ""
echo "   To add SSH forwarding for a VM manually:"
echo "   iptables -t nat -A PREROUTING -p tcp --dport 50000 -j DNAT --to 172.16.0.2:22"
echo "   iptables -t nat -A PREROUTING -p tcp --dport 50001 -j DNAT --to 172.16.0.3:22"

# Tap devices are created on-demand by server.py (_ensure_tap) when a VM boots
# and removed on destroy (_release_tap). No pre-creation needed.
echo "==> Tap devices will be created on-demand by server.py (no pre-creation)"
