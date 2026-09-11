#!/usr/bin/env bash
# Run the broker on your laptop against the REAL nonprod sandbox runtime and registry, using
# your own AWS credentials, so the whole path can be tested before any Runlayer wiring exists:
#   Claude Code -> http://localhost:8000/mcp -> AgentCore InvokeAgentRuntimeCommand -> microVM
#
# Auth is AUTH_MODE=agentcore-jwt (claims trusted without a signature) because only Runlayer
# can mint the production identity token. The dev token below just carries a sub/email so the
# registry has a user to key workspaces on. Never run the deployed broker in this mode.
#
#   bash deploy/local_broker.sh            # prints the dev token + claude mcp add command, then serves
#   bash deploy/local_broker.sh --token    # print the dev token only
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export AWS_PROFILE="${AWS_PROFILE:-aws-sr-am-admins@sr-es-devops-nonprod}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export AUTH_MODE=agentcore-jwt
export EXPECTED_ISSUER="${EXPECTED_ISSUER:-bashmcp-local-dev}"
export SANDBOX_RUNTIME_NAME="${SANDBOX_RUNTIME_NAME:-bashmcp_sandbox_nonprod}"
export SANDBOX_TABLE="${SANDBOX_TABLE:-bashmcp-sandboxes}"
export MAX_TIMEOUT="${MAX_TIMEOUT:-600}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
PORT=8000  # the broker always binds 8000
DEV_USER="${DEV_USER:-$(whoami)}"

TOKEN="$(python3 - "$DEV_USER" "$EXPECTED_ISSUER" <<'PY'
import base64, json, sys
def b64(o): return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()
user, iss = sys.argv[1], sys.argv[2]
print(f"{b64({'alg':'none','typ':'JWT'})}.{b64({'iss': iss, 'sub': f'local-dev-{user}', 'username': user, 'email': f'{user}@local.dev'})}.dev")
PY
)"
if [[ "${1:-}" == "--token" ]]; then echo "$TOKEN"; exit 0; fi

cat <<MSG

Local broker: http://localhost:${PORT}/mcp   (auth mode: agentcore-jwt, dev identity: local-dev-${DEV_USER})
Sandbox runtime: ${SANDBOX_RUNTIME_NAME}   Registry: ${SANDBOX_TABLE}   AWS profile: ${AWS_PROFILE}

In another terminal, connect Claude Code to it:

  claude mcp add --transport http bashmcp-local http://localhost:${PORT}/mcp \\
    --header "Authorization: Bearer ${TOKEN}"

then in Claude Code:  "use bashmcp-local to run: id -u && cat /etc/os-release | head -2 && df -h /mnt/workspace"

Or without Claude Code:

  curl -s http://localhost:${PORT}/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \\
    -H "Authorization: Bearer ${TOKEN}" \\
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"sandbox_exec","arguments":{"command":"id -u && hostname && echo hi > /mnt/workspace/local.txt"}}}'

Ctrl-C stops the broker. Remove the MCP entry later with:  claude mcp remove bashmcp-local
MSG
cd "$ROOT"
exec uv run python -m broker.app
