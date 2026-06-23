#!/usr/bin/env bash
# remote-smoke.sh — run FROM your Mac. Syncs this repo to the Firecracker host,
# runs host setup, starts the node-agent, and smoke-tests a real microVM end to end.
#
#   bash scripts/remote-smoke.sh [user@host]
#
# Default host is the fc-agent sandbox. Idempotent — safe to re-run.
set -euo pipefail

HOST="${1:-ec2-user@fc-agent-sandbox.stoneridgeam-dev.cloud}"
FC_BASE_DIR="/opt/fc-mcp"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/"   # repo root (this script lives in scripts/)

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

say "1/6  sync repo -> $HOST:bashmcp/"
rsync -az --exclude '.venv' --exclude 'data' --exclude '__pycache__' --exclude '.git' \
    "$SRC" "$HOST:bashmcp/"

say "2/6  host setup (idempotent; first run builds kernel + rootfs, ~5-10 min)"
ssh "$HOST" "cd ~/bashmcp && sudo FC_BASE_DIR=$FC_BASE_DIR bash scripts/setup-firecracker-al2.sh"

say "3/6  start node-agent (detached)"
ssh "$HOST" "cd ~/bashmcp && sudo FC_BASE_DIR=$FC_BASE_DIR bash scripts/start-detached.sh"

say "4/6  wait for readiness (up to 60s)"
ssh "$HOST" 'for i in $(seq 1 30); do
  if curl -sf localhost:8080/ready >/dev/null 2>&1; then
    echo "READY: $(curl -s localhost:8080/ready)"; exit 0
  fi
  sleep 2
done
echo "NOT READY after 60s. health: $(curl -s localhost:8080/health)"
echo "---- server log ----"; sudo tail -30 /tmp/fcmcp.log; exit 1'

say "5/6  boot a real microVM (session smoke-1) — proves Firecracker works"
ssh "$HOST" 'curl -s --max-time 120 -X POST localhost:8080/exec -H "Content-Type: application/json" -d "{\"session_id\":\"smoke-1\",\"command\":\"uname -a; head -1 /etc/os-release; hostname\"}"; echo'

say "6/6  reuse smoke-1 (same VM) + smoke-2 (new VM/slot) + list"
ssh "$HOST" 'echo "-- reuse smoke-1 --"; curl -s --max-time 60 -X POST localhost:8080/exec -H "Content-Type: application/json" -d "{\"session_id\":\"smoke-1\",\"command\":\"echo reused; ip -4 addr show eth0 | grep inet\"}"; echo; echo "-- smoke-2 (new VM) --"; curl -s --max-time 120 -X POST localhost:8080/exec -H "Content-Type: application/json" -d "{\"session_id\":\"smoke-2\",\"command\":\"ip -4 addr show eth0 | grep inet\"}"; echo; echo "-- /vms --"; curl -s localhost:8080/vms; echo'

say "done — server log: ssh $HOST 'sudo tail -50 /tmp/fcmcp.log'"
