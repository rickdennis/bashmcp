# P7 — agent-sessions HA validation (kind)

The agent-session REST surface (`/v1/agents`, `/v1/environments`, `/v1/sessions`) is forwarded
by the **router** to the node-agents. The router's cross-node forwarding + Session-CR routing
can only be exercised against a real cluster. This runbook validates it end-to-end in the
3-worker kind stack (run as root on the AL2+KVM box, same as `kind/kind-up.sh`).

## Prereqs

- `kind/kind-up.sh` has stood up the HA stack (router Deployment + node-agent StatefulSet + CRDs).
- The node-agent image includes this commit's `server.py` and the **router image** includes this
  commit's `proxy/router.py`. Rebuild both, then (StatefulSet `OnDelete`) roll each node-agent:
  `kubectl -n fc-mcp delete pod fc-node-agent-0 fc-node-agent-1 fc-node-agent-2`, and restart the
  router (`kubectl -n fc-mcp rollout restart deploy/fc-mcp-router`).
- An Anthropic key reaches each node at `/etc/fc-agent-runner/anthropic.env` (seeded like the VM
  images per node — see the kind seeding notes in CLAUDE.md).
- `R=http://<router-service-or-port-forward>:8080` ; `H='content-type: application/json'`.

## Checks

1. **Agent CRUD is forwarded + home-noded**
   ```bash
   AID=$(curl -s -X POST $R/v1/agents -H "$H" -d '{"name":"ha","system":"Be concise.","allowed_tools":["bash"]}' | jq -r .id)
   curl -s -X POST $R/v1/agents/$AID -H "$H" -d '{"allowed_tools":["bash","read"]}' >/dev/null  # -> version 2
   curl -s $R/v1/agents/$AID/versions | jq '.data | length'   # expect 2
   curl -s $R/v1/agents | jq '.data | length'                 # aggregated across nodes
   ```

2. **Session is placed (possibly on a different node than the agent) and pinned**
   ```bash
   S=$(curl -s -X POST $R/v1/sessions -H "$H" -d "{\"agent_id\":\"$AID\"}" --max-time 150)
   SID=$(echo "$S" | jq -r .id)
   kubectl -n fc-mcp get sessions | grep "${SID//_/-}"        # a Session CR named s-sesn-… exists
   NODE=$(kubectl -n fc-mcp get session s-${SID//_/-} -o jsonpath='{.spec.nodeName}'); echo "pinned to $NODE"
   ```

3. **Turn + multi-turn route to the pinned node**
   ```bash
   curl -s -X POST $R/v1/sessions/$SID/events -H "$H" -d '{"content":"Use the Bash tool to run uname -r; state the kernel."}' >/dev/null
   curl -sN --max-time 120 "$R/v1/sessions/$SID/events/stream" | grep -m1 '"type":"result"'
   curl -s $R/v1/sessions/$SID/usage | jq .usage.turns           # >= 1
   ```

4. **Failover**: cordon+delete the pinned node-agent pod; the liveness sweep marks the Session
   `Lost`; a fresh `POST /v1/sessions` places on a survivor. (Running VMs on the dead node are lost
   by design; survivors unaffected.)

5. **Teardown**: `curl -s -X DELETE $R/v1/sessions/$SID` → VM destroyed on the node + Session CR
   deleted (`kubectl -n fc-mcp get sessions` no longer lists it).

## Notes / known limits

- An agent/environment definition lives on **one** node (its home node). If that node dies, the
  definition is lost (running sessions elsewhere are unaffected) — consistent with the
  "VMs are pets, pinned to a node" philosophy. Recreate the agent to get a new home.
- The router exposes the agent loop over **REST** here; surfacing the `agent_run` /
  `agent_session_create` / `agent_send_message` MCP tools *through the router* (forwarding by
  Mcp-Session-Id) is a small follow-on — today only `bash_exec` is MCP-exposed at the router.
