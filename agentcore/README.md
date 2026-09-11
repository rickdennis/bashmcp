# bashmcp on Amazon Bedrock AgentCore

`sandbox_exec` for Claude Code (the Firecracker server's `bash_exec`), hosted on AgentCore instead of a self-managed Firecracker/KVM box.
Every caller gets a persistent, root-capable sandbox microVM; `/mnt/workspace` survives pauses.

```
Claude Code ──► Runlayer connector (manual OAuth 2.1 → Cognito, per-user login)
                     │  Authorization: Bearer <Cognito access token>
                     ▼
        AgentCore Runtime  bashmcp_broker   MCP protocol, :8000/mcp, stateless FastMCP
        customJWTAuthorizer(Cognito) · requestHeaderAllowlist=[Authorization]
                     │  JWT sub → DynamoDB (user, workspace) → runtimeSessionId
                     │  InvokeAgentRuntimeCommand / StopRuntimeSession
                     ▼
        AgentCore Runtime  bashmcp_sandbox  HTTP protocol, IAM inbound (broker role only)
        one microVM per session · commands run as uid 0 · /mnt/workspace = session storage
```

| Firecracker design (`../server.py`) | Here |
|---|---|
| microVM per session, SSH as root | Runtime session microVM, `InvokeAgentRuntimeCommand` (root, streaming) |
| overlay ext4 | managed session storage at `/mnt/workspace` (1 GB, 14-day idle expiry) |
| memory+disk snapshot pause/resume | `StopRuntimeSession`; **disk only**, resume is implicit on next command |
| `vm-state.json` + `session-vm.json` | DynamoDB `bashmcp-sandboxes` (pk `user_sub`, sk `workspace`) |
| idle pause loop | `lifecycleConfiguration.idleRuntimeSessionTimeout` |
| no auth | Cognito JWT on the broker; IAM on the sandbox |

## Layout

```
broker/     FastMCP server: sandbox_exec, sandbox_list/status/pause/new/destroy  (+ Dockerfile)
sandbox/    Ubuntu 24.04 image with dev tools + a no-op AgentCore entrypoint  (+ Dockerfile)
runlayer.yaml  Runlayer Deploy manifest for the broker (hosting A)
deploy/     build_push.sh, probe_sandbox.py, list_sandboxes.py, _common.py; standalone/ = original boto3 + Cognito path
ui/         local operator web UI (127.0.0.1): all sandboxes, runtime status, recent commands, exec/pause
smoke.py    end-to-end test against the deployed broker
```

## Tools

| Tool | What it does |
|---|---|
| `sandbox_exec(command, working_dir="/mnt/workspace", timeout=60, workspace="default")` | Runs `/bin/bash -lc` in your sandbox as root. Creates the sandbox on first use, resumes it if stopped. Returns `{stdout, stderr, returncode, elapsed_seconds, status, cold_start, runtime_session_id, workspace}`. |
| `sandbox_list()` | Your workspaces. |
| `sandbox_status(workspace)` | Metadata plus `likely_stopped` (inferred from idle time; AgentCore has no session status API). |
| `sandbox_pause(workspace)` | `StopRuntimeSession` now. Files under `/mnt/workspace` are kept, processes are not. |
| `sandbox_new(workspace, label)` | A fresh, empty sandbox under a new name. |
| `sandbox_destroy(workspace)` | Stop and forget. Storage is reclaimed by AgentCore after 14 idle days. |

`sandbox_exec` was named `bash_exec` until 2026-09-11 (the Firecracker server in the repo root keeps
that name). Each `sandbox_exec` is a fresh bash process: chain with `&&`/`;` for state. `HOME` is redirected to
`/mnt/workspace/.home` so dotfiles, git config and tool caches survive a pause. Anything outside
`/mnt` (apt installs, `/root`, running processes) is lost when the microVM stops. Bake tools into
`sandbox/Dockerfile` instead.

## Deploying

Runlayer cannot call AgentCore directly (AgentCore accepts only SigV4 or an IdP JWT), so the
broker is the piece in between. Two hostings are supported by the same image and code:

### A. Broker on Runlayer Deploy (recommended, fewest moving parts)

`runlayer.yaml` in this directory. Runlayer builds and runs the broker, registers it as a
connector, injects identity per request, and gives the task an ECS role that may only
`sts:AssumeRole` the customer role `bashmcp-broker-runlayer-nonprod` (ExternalId = deployment id).
No EKS, no PrivateLink, no IRSA, no shim.

| Where | What |
|---|---|
| `devops-live` `us-east-1/nonprod/bashmcp-agentcore.tf` | ECR repos, DynamoDB `bashmcp-sandboxes`, sandbox runtime `bashmcp_sandbox_nonprod`, sandbox execution role, Runlayer-trusted broker role (gated on `bashmcp-runlayer-deployment-id`) |
| this directory | `runlayer.yaml` (deployment manifest), `broker/` (image), `deploy/build_push.sh`, `deploy/probe_sandbox.py` |

1. `uvx runlayer login --host https://stoneridge.runlayer.com`, then
   `uvx runlayer deploy init --host https://stoneridge.runlayer.com --config runlayer.yaml`
   (name `bashmcp`). Paste the issued UUID into `runlayer.yaml` `id:`.
2. devops-live PR setting `bashmcp-runlayer-deployment-id` to that UUID in `bashmcp.auto.tfvars.json`
   (merge applies; creates the role whose trust is pinned to Runlayer's per-deployment task role).
3. `uvx runlayer deploy --config runlayer.yaml --host https://stoneridge.runlayer.com` from this
   directory. First deploy activates the connector.
4. In Runlayer: enable Identity Forward **signed identity token** on the connector, grant access.
   Pin `RUNLAYER_AUDIENCE` in `runlayer.yaml` to `runlayer:identity-forward:<connector-id>` and redeploy.
5. `claude mcp add --transport http bashmcp https://stoneridge.runlayer.com/api/v1/proxy/<connector-id>/mcp`
   and ask Claude Code to run something.

### B. Broker on EKS behind a PrivateLink shim (retired 2026-09-11)

The first working deployment ran the broker on eks-devops-nonprod behind the mcp-server
PrivateLink and a passthrough Runlayer shim, with the IRSA role `bashmcp-broker-nonprod`. It
worked, but cost an EKS app, the PrivateLink path (350 s NLB idle limit), and a shim. It was
retired once A was verified; the manifests live in k8s-devops history (PR #244) if ever needed.

Broker environment: `AUTH_MODE=runlayer`, `RUNLAYER_URL`, optional `RUNLAYER_AUDIENCE`,
optional `BROKER_SHARED_BEARER`, `SANDBOX_RUNTIME_NAME` (resolved to an ARN at startup) or
`SANDBOX_ARN`, `SANDBOX_TABLE`, `MAX_TIMEOUT`; for Runlayer hosting the role is picked up from
`RUNLAYER_AWS_ROLE_BASHMCP` + `RUNLAYER_DEPLOYMENT_ID` (or `AWS_ASSUME_ROLE_ARN` +
`AWS_ASSUME_ROLE_EXTERNAL_ID`).

Images: `bash deploy/build_push.sh --tag <version>` pushes `devops/bashmcp-sandbox` (arm64) and
`devops/bashmcp-broker` (multi-arch) to the nonprod-account ECR (immutable tags). The sandbox tag
is pinned in devops-live `bashmcp.auto.tfvars.json`; bumping it wipes every session's workspace.
Runlayer Deploy builds the broker image itself from `broker/Dockerfile`, so the ECR broker image
is only needed for hosting B.

Sandbox check with your own credentials (starts one microVM session):
`uv run deploy/probe_sandbox.py --name bashmcp_sandbox_nonprod --profile aws-sr-am-admins@sr-es-devops-nonprod`.

## Standalone path (boto3 scripts, AgentCore-hosted broker)

`deploy/standalone/` keeps the original all-on-AgentCore variant: broker as a second AgentCore
runtime with a Cognito `customJWTAuthorizer` (`AUTH_MODE=agentcore-jwt`) and a Runlayer connector
using manual OAuth. Scripts `01_iam` ... `07_teardown` all support `--dry-run`;
`smoke_cognito.py` is its end-to-end test. Useful for a sandbox account without the PrivateLink
plumbing; not the firm pattern.

```bash
cd agentcore
uv sync
uv run pytest -q
```

## Choosing a workspace (per machine, per project, per person)

The broker keys sandboxes by **Runlayer user** and a **workspace name**, never by client or
conversation, so the same person moving between claude.ai, Claude Desktop and Claude Code lands in
the same sandboxes. Nothing that reaches the broker identifies a Claude conversation (verified
exhaustively: Anthropic's `traceparent` scopes one assistant turn, Runlayer's ids scope one call).
The workspace resolves in this order:

1. the `workspace` argument on the tool call (explicit, chosen by you or by the model);
2. the optional `X-Bashmcp-Workspace` request header (Runlayer forwards client headers verbatim;
   only Claude Code can set one, so avoid it if you move between clients);
3. `"default"`.

Cross-client habit that works everywhere: name sandboxes after projects, and start a new
conversation with `sandbox_list` (or tell Claude the name). Optional, Claude-Code-only pinning:

```bash
# one sandbox per machine (user scope)
claude mcp add --transport http --scope user bashmcp \
  https://stoneridge.runlayer.com/api/v1/proxy/105fb9c5-bd09-4ea1-8e31-68bf8f89c7b5/mcp \
  --header "X-Bashmcp-Workspace: rick-laptop"
```

```json
// one sandbox per repo: .mcp.json in the project
{"mcpServers": {"bashmcp": {"type": "http",
  "url": "https://stoneridge.runlayer.com/api/v1/proxy/105fb9c5-bd09-4ea1-8e31-68bf8f89c7b5/mcp",
  "headers": {"X-Bashmcp-Workspace": "${BASHMCP_WORKSPACE:-my-repo}"}}}}
```

Claude Code's `headersHelper` can also compute the header at connection time (for example from the
git remote name). claude.ai connectors cannot set custom headers, so web sessions use `default`
unless the model passes `workspace` explicitly (a project instruction is the reliable way).

## Operating notes

- **Limits per session:** 2 vCPU / 8 GB, 8 h max microVM lifetime (a new one is provisioned
  transparently), 1 GB storage, ~100–200k files, no hard links / xattr / fallocate.
- **Cold start:** the first command on a new or stopped workspace boots a microVM and restores
  storage (seconds). AgentCore may answer 409 `Session operation in progress` meanwhile; the
  broker retries with backoff for up to 90 s. `cold_start: true` in the result flags it.
- **Idle stop:** sandbox 30 min (`--sandbox-idle`), broker 15 min. Both resume on demand.
- **Identity:** in `runlayer` mode the broker verifies the Identity Forward JWT itself (EdDSA,
  JWKS, issuer, audience, 5-minute expiry). In `agentcore-jwt` mode it trusts the Authorization
  JWT only because the Runtime's authorizer verified it. The sandbox runtime accepts IAM only;
  only the broker's role holds `InvokeAgentRuntimeCommand` on it.
- **Egress:** the sandbox runs in `PUBLIC` network mode in this prototype (the old `fc-egress`
  allow-listing is not ported). Root inside the sandbox can read its execution role's
  credentials, which is why that role can only pull its image and write logs. VPC mode plus
  a resource-based policy on the sandbox runtime is the follow-on.
- **Audit:** CloudTrail records every `InvokeAgentRuntimeCommand`; the sandbox's CloudWatch log
  group (`/aws/bedrock-agentcore/runtimes/bashmcp_sandbox-*`) records the command text; the
  broker logs user, workspace, exit code and the first 200 chars of each command.
- **Operator view:** AgentCore has no session-list API, so the DynamoDB registry is the source of
  truth. `uv run deploy/list_sandboxes.py` prints it; `uv run ui/app.py --profile <profile>` serves a
  local web UI on http://127.0.0.1:8787 that also shows the runtime status, the recent-command feed
  from CloudWatch, and lets you run a command in or pause any user's sandbox with your own AWS
  credentials (root inside their microVM; every call is in CloudTrail). See `ui/README.md`.
- **Cost:** microVM CPU $0.0895/vCPU-h while busy plus $0.00945/GB-h of peak memory while the
  session is up (idle-but-not-stopped bills memory only). Session storage pricing is TBD (preview).

## Known risks / to verify at the checkpoints

- Runlayer ↔ Cognito OAuth: Runlayer must send the **access** token (has `client_id`); if it
  forwards the ID token instead, switch the authorizer to `allowedAudience`.
- Broker stickiness: if Runlayer does not replay AgentCore's `Mcp-Session-Id`, every call pays a
  broker cold start (latency only; the broker is stateless).
- Runlayer/Claude Code HTTP timeouts may be shorter than the 600 s `sandbox_exec` cap.
