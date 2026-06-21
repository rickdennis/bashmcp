#!/usr/bin/env bash
# kind-up.sh — stand up the full HA stack in kind on a Linux+KVM host.
# Run as root ON the host:  sudo bash kind/kind-up.sh
#
# Creates a 3-worker kind cluster (with /dev/kvm + /dev/net/tun in each worker),
# builds + loads the image, deploys CRDs + StatefulSet + router, and verifies a
# real Firecracker microVM boots inside a kind node. Idempotent.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER=fc-mcp
NS=fc-mcp
IMAGE=fc-bash-mcp:latest
WORKERS=3
KIND_VERSION="${KIND_VERSION:-v0.24.0}"
ARCH=$(uname -m); case "$ARCH" in x86_64) GOARCH=amd64;; aarch64) GOARCH=arm64;; *) GOARCH=amd64;; esac
export PATH="/usr/local/bin:$PATH"   # AL2 sudo secure_path excludes it

log() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "run as root: sudo bash kind/kind-up.sh"
[[ -e /dev/kvm ]]      || die "/dev/kvm not found on host"
command -v docker >/dev/null || die "docker required (run setup-firecracker-al2.sh first)"

# kind multi-node commonly fails to join workers when inotify is exhausted
# (kubelet can't register; "error uploading crisocket"). Raise the limits.
sysctl -w fs.inotify.max_user_watches=1048576  >/dev/null 2>&1 || true
sysctl -w fs.inotify.max_user_instances=8192   >/dev/null 2>&1 || true

# This host is cgroup v1 with an old systemd; the systemd cgroup driver makes the
# kind node kubelets crash-loop creating kubepods.slice. Pin Docker to cgroupfs to
# match the kind kubelet/containerd (cgroupfs, set via kind-cluster.yaml patches).
log "0/8  pin Docker to the cgroupfs cgroup driver"
if grep -q 'native.cgroupdriver=cgroupfs' /etc/docker/daemon.json 2>/dev/null; then
  echo "(already cgroupfs)"
else
  mkdir -p /etc/docker
  printf '{\n  "exec-opts": ["native.cgroupdriver=cgroupfs"]\n}\n' > /etc/docker/daemon.json
  echo "set docker cgroupdriver=cgroupfs; restarting docker"
  systemctl restart docker
  for _ in $(seq 1 15); do docker info >/dev/null 2>&1 && break; sleep 1; done
fi

log "1/8  ensure kind + kubectl"
if ! command -v kind >/dev/null; then
  curl -fsSLo /usr/local/bin/kind "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-${GOARCH}"
  chmod +x /usr/local/bin/kind
fi
if ! command -v kubectl >/dev/null; then
  KV=$(curl -fsSL https://dl.k8s.io/release/stable.txt)
  curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KV}/bin/linux/${GOARCH}/kubectl"
  chmod +x /usr/local/bin/kubectl
fi
echo "kind $(kind version | head -1 || true); kubectl present"

log "2/8  create cluster '$CLUSTER' (3 workers, /dev/kvm passthrough)"
create() { kind create cluster --name "$CLUSTER" --config "$REPO_DIR/kind/kind-cluster.yaml" --retain --wait 150s; }
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  if kubectl --context "kind-$CLUSTER" get nodes >/dev/null 2>&1; then
    echo "(healthy cluster exists)"
  else
    echo "(existing cluster unhealthy — recreating)"
    kind delete cluster --name "$CLUSTER"
    create
  fi
else
  create
fi

log "3/8  build + load image $IMAGE"
docker build -t "$IMAGE" "$REPO_DIR"
kind load docker-image "$IMAGE" --name "$CLUSTER"

log "4/8  label workers fc-mcp=true"
kubectl label nodes --selector='!node-role.kubernetes.io/control-plane' fc-mcp=true --overwrite

log "4b/8 seed kernel/rootfs + SSH key onto workers (/opt/fc-seed)"
# The node-agent pods get a fresh, empty local PV; they mount the shared kernel
# + base rootfs + matching private key from the node's /opt/fc-seed (hostPath).
# All kind nodes are on this one host, which already built these under FC_BASE_DIR.
SEED_DIR="${FC_BASE_DIR:-/opt/fc-mcp}"
[[ -f "$SEED_DIR/vm-images/vmlinux-5.10" ]] || die "missing $SEED_DIR/vm-images — run setup-firecracker-al2.sh first"
[[ -f "$SEED_DIR/vm_ssh_key" ]]            || die "missing $SEED_DIR/vm_ssh_key"
for n in $(kind get nodes --name "$CLUSTER" | grep -- '-worker'); do
  docker exec "$n" mkdir -p /opt/fc-seed
  docker cp "$SEED_DIR/vm-images"  "$n":/opt/fc-seed/
  docker cp "$SEED_DIR/vm_ssh_key" "$n":/opt/fc-seed/vm_ssh_key
  echo "  seeded $n"
done

log "5/8  apply CRDs + namespace + manifests"
kubectl apply -f "$REPO_DIR/deploy/crds/"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$REPO_DIR/kubernetes/"
kubectl -n "$NS" scale statefulset/fc-node-agent --replicas="$WORKERS"

log "6/8  wait for rollouts"
# The node-agent StatefulSet uses OnDelete (not RollingUpdate), so `kubectl rollout
# status` errors on it ("only available for RollingUpdate strategy type") and would
# abort under `set -e`. Poll readyReplicas instead. The router IS a Deployment, so
# its rollout status works normally.
echo "waiting for $WORKERS node-agent pod(s) to be Ready ..."
for _ in $(seq 1 72); do
  ready=$(kubectl -n "$NS" get statefulset/fc-node-agent -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
  [[ "${ready:-0}" -ge "$WORKERS" ]] && break
  sleep 5
done
ready=$(kubectl -n "$NS" get statefulset/fc-node-agent -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
echo "node-agent readyReplicas=${ready:-0}/$WORKERS"
[[ "${ready:-0}" -ge "$WORKERS" ]] || die "node-agents did not become Ready (see: kubectl -n $NS get pods)"
# NOTE: OnDelete means a rebuilt image does NOT roll existing pods. After re-running
# with code changes, `kubectl -n $NS delete pod fc-node-agent-N` to pick up the new
# image (destroys that pod's VMs — fine for dev).
kubectl -n "$NS" rollout status deploy/fc-mcp-router --timeout=180s

log "7/8  cluster state"
kubectl -n "$NS" get pods -o wide
echo "--- nodeagents (capacity/heartbeat) ---"; kubectl get nodeagents -A || true
echo "--- sessions ---"; kubectl get sessions -A || true

log "8/8  smoke: boot a real Firecracker VM inside a kind node (direct /exec)"
POD=$(kubectl -n "$NS" get pods -l app=fc-node-agent -o jsonpath='{.items[0].metadata.name}')
echo "exec into $POD ..."
kubectl -n "$NS" exec "$POD" -- sh -c \
  'curl -s --max-time 120 -X POST localhost:8080/exec -H "Content-Type: application/json" -d "{\"session_id\":\"kind-smoke\",\"command\":\"uname -a; head -1 /etc/os-release; hostname\"}"' \
  || echo "(direct /exec smoke failed — check: kubectl -n $NS logs $POD)"
echo

cat <<EOF

──────────────────────────────────────────────────────────────────────
HA stack is up. Next:
  kubectl -n $NS get pods -o wide
  kubectl get nodeagents -A          # per-node freeTaps + heartbeat
  kubectl get sessions -A            # session -> node bindings (after MCP traffic)
  kubectl -n $NS logs deploy/fc-mcp-router

End-to-end MCP test through the router (separate step):
  kubectl -n $NS port-forward svc/fc-mcp-router 18080:8080 &
  uv run python kind/mcp_smoke.py http://localhost:18080/mcp 3
  kubectl get sessions -A            # should show 3 sessions placed across workers

Tear down:  kind delete cluster --name $CLUSTER
──────────────────────────────────────────────────────────────────────
EOF
