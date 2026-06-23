#!/usr/bin/env bash
# start-detached.sh — start the node-agent server detached (run ON the host, as root).
#   sudo FC_BASE_DIR=/opt/fc-mcp bash scripts/start-detached.sh
# Kills any prior server, then launches a fresh one under setsid with redirected
# FDs so it survives the SSH session. Log: /tmp/fcmcp.log
set -euo pipefail
# cd to the repo root that holds server.py: this script's own dir (image: flat /app) or its
# parent (repo: scripts/ -> root).
_d="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "$_d/server.py" ]] || _d="$(cd "$_d/.." && pwd)"
cd "$_d"

FC_BASE_DIR="${FC_BASE_DIR:-/opt/fc-mcp}"
MCP_PORT="${MCP_PORT:-8080}"
# AL2 sudo secure_path drops /usr/local/bin where uv lives — resolve explicitly.
UV="$(command -v uv || echo /usr/local/bin/uv)"

pkill -f 'server.py' 2>/dev/null || true
sleep 1
setsid env FC_BASE_DIR="$FC_BASE_DIR" "$UV" run python server.py --port "$MCP_PORT" \
    >/tmp/fcmcp.log 2>&1 </dev/null &
sleep 2
echo "node-agent started (FC_BASE_DIR=$FC_BASE_DIR port=$MCP_PORT log=/tmp/fcmcp.log)"
