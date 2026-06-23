#!/usr/bin/env bash
# kind-debug.sh — create the cluster with --retain and dump the real kubelet/
# containerd logs from the control-plane node, so we see WHY the kubelet is
# unhealthy (kubeadm's "required cgroups disabled" is just a generic hint).
# Run as root on the host. Leaves the (possibly failed) cluster for inspection.
set -uo pipefail
export PATH="/usr/local/bin:$PATH"
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

kind delete cluster --name fc-mcp >/dev/null 2>&1 || true

echo "=== creating cluster (retain) ==="
kind create cluster --name fc-mcp --config kind/kind-cluster.yaml --retain --wait 90s
echo "CREATE_RC=$?"

echo "=== fc-mcp containers ==="
docker ps -a --format '{{.Names}}\t{{.Status}}' | grep fc-mcp || true

echo "=== control-plane kubelet (tail 80) ==="
docker exec fc-mcp-control-plane journalctl -u kubelet --no-pager 2>&1 | tail -80 || echo "(no cp container)"

echo "=== control-plane containers (crictl) ==="
docker exec fc-mcp-control-plane crictl ps -a 2>&1 | head -20 || true

echo "=== containerd (tail 20) ==="
docker exec fc-mcp-control-plane journalctl -u containerd --no-pager 2>&1 | tail -20 || true

echo "===DONE==="
