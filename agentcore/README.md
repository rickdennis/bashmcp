# bashmcp on Amazon Bedrock AgentCore

`bash_exec` for Claude Code, hosted on AgentCore instead of a self-managed Firecracker/KVM box.
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
broker/     FastMCP server: bash_exec, sandbox_list/status/pause/new/destroy  (+ Dockerfile)
sandbox/    Ubuntu 24.04 image with dev tools + a no-op AgentCore entrypoint  (+ Dockerfile)
deploy/     build_push.sh, probe_sandbox.py, _common.py; standalone/ = original boto3 + Cognito path
smoke.py    end-to-end test against the deployed broker
```

## Tools

| Tool | What it does |
|---|---|
| `bash_exec(command, working_dir="/mnt/workspace", timeout=60, workspace="default")` | Runs `/bin/bash -lc` in your sandbox as root. Creates the sandbox on first use, resumes it if stopped. Returns `{stdout, stderr, returncode, elapsed_seconds, status, cold_start, runtime_session_id, workspace}`. |
| `sandbox_list()` | Your workspaces. |
| `sandbox_status(workspace)` | Metadata plus `likely_stopped` (inferred from idle time; AgentCore has no session status API). |
| `sandbox_pause(workspace)` | `StopRuntimeSession` now. Files under `/mnt/workspace` are kept, processes are not. |
| `sandbox_new(workspace, label)` | A fresh, empty sandbox under a new name. |
| `sandbox_destroy(workspace)` | Stop and forget. Storage is reclaimed by AgentCore after 14 idle days. |

Each `bash_exec` is a fresh bash process: chain with `&&`/`;` for state. `HOME` is redirected to
`/mnt/workspace/.home` so dotfiles, git config and tool caches survive a pause. Anything outside
`/mnt` (apt installs, `/root`, running processes) is lost when the microVM stops. Bake tools into
`sandbox/Dockerfile` instead.

## Deploying (GitOps, recommended)

The firm's pattern for exposing an MCP server to Runlayer is a PrivateLink shim, so the
broker runs on **eks-devops-nonprod** and only the sandbox runtime lives on AgentCore. Cognito
is not needed: the broker verifies Runlayer's Identity Forward JWT (`x-runlayer-identity-token`,
EdDSA, tenant JWKS) and takes the caller's email from it.

| Repo / branch | Contents |
|---|---|
| ECR (see note) | repositories `devops/bashmcp-sandbox`, `devops/bashmcp-broker` in the mgmt account |
| `devops-live` `bashmcp-agentcore` | `us-east-1/nonprod/bashmcp-agentcore.tf`: sandbox runtime `bashmcp_sandbox_nonprod`, execution role, DynamoDB `bashmcp-sandboxes`; image tag in `bashmcp.auto.tfvars.json` |
| `admin-live` `bashmcp-broker-irsa` | `accounts/sr-es-devops-nonprod/us-east-1/nonprod/iam_bashmcp_broker.tf`: IRSA role `bashmcp-broker-nonprod` |
| `k8s-devops` `bashmcp-broker` | `apps/bashmcp/us-east-1/nonprod/bashmcp.yaml` + `appsets-nonprod/bashmcp.yaml`: Deployment, SA, HTTPRoutes on `eg` and `eg-privatelink`, health check |
| `runlayer-shims-live` `bashmcp-shim` (local) | `environments/nonprod/bashmcp.yaml`: passthrough shim on the `mcp-test-nonprod` PrivateLink connection |

Rollout order (each step is a PR; `devops-live` applies on merge to master):

1. Create the two ECR repositories (`ecr-live` is archived; see the note in this repo's
   GitOps summary), then `bash deploy/build_push.sh --tag 0.1.0` (pushes `devops/bashmcp-sandbox`
   arm64 and `devops/bashmcp-broker` multi-arch to the mgmt ECR).
2. `devops-live` PR (plan appears on the PR; merge applies). Verify with
   `uv run deploy/probe_sandbox.py --name bashmcp_sandbox_nonprod` using your own credentials.
3. `admin-live` PR (IRSA role), then `k8s-devops` PR (ArgoCD syncs; check `/healthz` on
   `https://bashmcp.stoneridgeam-nonprod.cloud/healthz` from inside the VPC).
4. `runlayer-shims-live`: run `uvx runlayer deploy init` once, paste the UUID into the YAML, PR;
   merge deploys and registers the connector. Enable **Identity Forward (signed token)** on
   the connector, grant access with `src/scripts/access.sh`, then set `RUNLAYER_AUDIENCE` to
   `runlayer:identity-forward:<connector-id>` in the k8s manifest.
5. `claude mcp add --transport http bashmcp https://stoneridge.runlayer.com/api/v1/proxy/<connector-id>/mcp`
   and ask Claude Code to run something.

Broker environment: `AUTH_MODE=runlayer`, `RUNLAYER_URL`, optional `RUNLAYER_AUDIENCE`,
optional `BROKER_SHARED_BEARER` (pair with the shim's `UPSTREAM_BEARER`), `SANDBOX_RUNTIME_NAME`
(resolved to an ARN at startup) or `SANDBOX_ARN`, `SANDBOX_TABLE`, `MAX_TIMEOUT` (300 in
nonprod: the PrivateLink NLB idles out at 350 s and responses are not streamed).

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
- **Cost:** microVM CPU $0.0895/vCPU-h while busy plus $0.00945/GB-h of peak memory while the
  session is up (idle-but-not-stopped bills memory only). Session storage pricing is TBD (preview).

## Known risks / to verify at the checkpoints

- Runlayer ↔ Cognito OAuth: Runlayer must send the **access** token (has `client_id`); if it
  forwards the ID token instead, switch the authorizer to `allowedAudience`.
- Broker stickiness: if Runlayer does not replay AgentCore's `Mcp-Session-Id`, every call pays a
  broker cold start (latency only; the broker is stateless).
- Runlayer/Claude Code HTTP timeouts may be shorter than the 600 s `bash_exec` cap.
