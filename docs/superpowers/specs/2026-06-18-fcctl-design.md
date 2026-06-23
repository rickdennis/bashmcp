# fcctl — admin CLI for the fc-mcp HA stack

**Date:** 2026-06-18
**Status:** Design approved (pending written-spec review)

## Purpose

A single command-line tool, `fcctl`, that performs the day-to-day operational tasks
we have been doing by hand against the fc-mcp HA cluster: listing/inspecting VMs,
sessions, and node-agents; managing VM lifecycle (create/destroy/pause/resume/restore);
running a command in a VM; bulk reset/drain; and a live `top` dashboard (the work
`fc-top.py` does today). It replaces the ad-hoc `fc-top.py` script and the throwaway
`cleanup_fresh.py` with one durable, distributable binary.

Written in **Go**, shipped as a single static binary you `scp` to a host that has
`kubectl` + a kubeconfig and network reachability to the node-agents.

## Scope

**In scope (v1):**
- Inspection: `ls vms|sessions|nodes`, `get vm|session|node <id>`
- Lifecycle: `create`, `destroy`, `pause`, `resume`, `restore`
- `exec <vm_id> -- <cmd>`
- Bulk: `reset` (destroy all VMs + delete all Session CRs), `drain <node>`
- `top` — live TUI dashboard
- One server-side addition: `POST /vms/{vm_id}/exec` on `server.py`

**Out of scope (v1):**
- Mutating Kubernetes CRs other than deleting Sessions during `reset` (no Session
  create/edit — the router owns those)
- Auth/RBAC inside `fcctl` (it inherits the host's kubeconfig + network access)
- Cross-node VM migration, S3 bucket management, IAM changes
- Windows/macOS target builds (binary targets linux/amd64 — the cluster hosts)

## Architecture

`fcctl` is a `cobra`-based CLI. It reaches the cluster two ways, mirroring the
patterns `fc-top.py` and `cleanup_fresh.py` already use:

1. **Kubernetes CRs** (`sessions`, `nodeagents`, the router `lease`) — by shelling
   out to `kubectl get … -o json` and unmarshalling. Chosen over `client-go` to keep
   the binary small and match the existing tooling; the hosts always have `kubectl`.
2. **Per-VM operations** — direct REST to each node-agent at `http://<podIP>:8080`
   over `net/http`. The node-agent `podIP` comes from each `NodeAgent` CR's
   `spec.podIP`. **No dependency on the router** (the router is the MCP data plane for
   clients; `fcctl` is the admin/control plane).

```
fcctl (host binary)
 ├── kubectl --kubeconfig <path> get nodeagents|sessions|lease -o json  → cluster state
 └── HTTP → node-agent REST (172.18.0.x:8080)                            → VM lifecycle/exec
              /vms (GET), /vms/{id} (GET, DELETE),
              /vms/{id}/pause|resume|exec (POST),
              /vms (POST create), /restore (POST), /drain (POST)
```

### Module layout (`fcctl/`, its own Go module)

| File | Responsibility |
|------|----------------|
| `main.go` | `cobra` root command, global flags, wiring |
| `kube.go` | shell `kubectl` — **always** with `--kubeconfig <path>` (never ambient) — and unmarshal NodeAgent/Session/Lease CRs into structs |
| `client.go` | node-agent REST client (typed; one method per endpoint) |
| `index.go` | build a `vm_id → (node, podIP)` index by fanning `/vms` across node-agents (goroutines + `errgroup`) |
| `render.go` | `lipgloss` tables + summary line, shared by `ls`/`get`/`top` |
| `tui.go` | `bubbletea` model for `fcctl top` (ticker refresh, `q` quits) |
| `cmd_*.go` | one file per command group (inspect, lifecycle, exec, bulk) |

Module path: `fcctl` (local module; not published). Go 1.22+.

### Dependencies
- `github.com/spf13/cobra` — command tree
- `github.com/charmbracelet/bubbletea` + `github.com/charmbracelet/lipgloss` — TUI + tables
- stdlib `net/http`, `os/exec`, `encoding/json`, `golang.org/x/sync/errgroup`

## Data model (Go structs mirror the live JSON)

From `GET /vms` (per node-agent), aggregated with the source node:
```go
type VM struct {
    VMID        string  `json:"vm_id"`
    Name        string  `json:"name"`
    Status      string  `json:"status"`      // creating|booting|running|paused|error|destroyed
    VCPU        int     `json:"vcpu"`
    MemMB       int     `json:"mem_mb"`
    IPAddress   string  `json:"ip_address"`
    HasSnapshot bool    `json:"has_snapshot"`
    CreatedAt   float64 `json:"created_at"`  // epoch seconds
    Node        string  `json:"-"`           // filled by fcctl from which node-agent answered
}
```
`get vm <id>` additionally surfaces `disk_mb`, `pid`, `snapshot{created_at,mem_size_mb}`,
and `error` from `GET /vms/{id}`.

CRs (subset, via `kubectl`):
- **NodeAgent**: `spec.nodeName`, `spec.podIP`, `spec.maxVms`; `status.phase`,
  `status.freeTaps`, `status.heartbeatTime`
- **Session**: `spec.mcpSessionId`, `spec.nodeName`; `status.phase`, `status.vmRef`
- **Lease** (`fc-mcp-router-leader`): `spec.holderIdentity` → current router leader

## Command surface

Resolution rule: any command that takes a `<vm_id>` first builds the `vm_id → node`
index (one `/vms` fan-out) to find the owning node-agent, then calls that node.

| Command | Action | Calls |
|---------|--------|-------|
| `fcctl ls vms` | aggregate VMs across all nodes, table | `kubectl get nodeagents` → each `GET /vms` |
| `fcctl ls sessions` | sessions table | `kubectl get sessions` |
| `fcctl ls nodes` | node-agent capacity/phase/heartbeat table | `kubectl get nodeagents` (+ live `/vms` counts) |
| `fcctl get vm <id>` | full VM detail (status, vcpu/mem/disk, ip, pid, snapshot, error, node) | resolve → `GET /vms/{id}` |
| `fcctl get session <id>` | full Session CR detail: mcpSessionId, node, phase, vmRef, age (vmRef may be blank — see note) | `kubectl get session` |
| `fcctl get node <name>` | one node-agent's detail: phase, freeTaps/maxVms, heartbeat age, podIP, reachability, and its VMs broken out | `kubectl get nodeagent` + that node's `GET /vms` |
| `fcctl create [--node N] [--name] [--vcpu] [--mem-mb] [--disk-mb]` | create a VM (node chosen by free capacity if `--node` omitted) | `POST /vms` on target node |
| `fcctl exec <id> [--timeout] [--workdir] -- <cmd>` | run a command in a VM | resolve → `POST /vms/{id}/exec` |
| `fcctl pause <id>` | snapshot+pause | resolve → `POST /vms/{id}/pause` |
| `fcctl resume <id>` | resume from snapshot | resolve → `POST /vms/{id}/resume` |
| `fcctl restore <id> --node N` | restore from S3 onto node N (needs `FC_S3_BUCKET` on the agent) | `POST /restore {vm_id}` on node N |
| `fcctl destroy <id>` | destroy a VM | resolve → `DELETE /vms/{id}` |
| `fcctl drain <node>` | pause+snapshot every running VM on a node | `POST /drain` on that node |
| `fcctl reset` | destroy ALL VMs on ALL nodes, then delete ALL Session CRs | each `DELETE /vms/{id}` + `kubectl delete sessions --all` |
| `fcctl top` | live dashboard | same gather as `ls`, on a ticker |

### Node selection for `create`
If `--node` omitted, pick the Ready node-agent with the most `status.freeTaps`
(ties broken by name). Error if none has capacity.

### Inspection detail (`get`)
- `get vm <id>` and `get node <name>` reach the owning/named node-agent for live
  data, so they reflect actual VM state, not just the CR.
- `get session <id>` reads only the Session CR (the node-agents don't expose the
  session→VM map over REST). Today the router leaves `status.vmRef` empty, so the
  bound VM column is shown as blank rather than guessed. If/when `vmRef` is populated
  (a known gap, out of scope here), `get session` will resolve and show the VM with
  no further change. `<id>` accepts either the bare `mcpSessionId` or the `s-<id>`
  CR name.

## Server change: `POST /vms/{vm_id}/exec`

The only change outside the Go tool. Mirrors `bash_exec` minus session resolution,
reusing the existing `_ssh_exec(vm_id, command, timeout)` helper and `working_dir`
convention. New input model + endpoint:

```python
class VmExecInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    command: str = Field(..., min_length=1, max_length=8192)
    timeout: int = Field(default=60, ge=1, le=600)
    working_dir: Optional[str] = Field(default=None)

@api.post("/vms/{vm_id}/exec", tags=["VMs"], summary="Run a command in a VM")
async def api_vm_exec(vm_id: str, params: VmExecInput):
    record = _vm_state.get(vm_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"VM '{vm_id}' not found.")
    if record["status"] != "running":
        raise HTTPException(status_code=409,
                            detail=f"VM is not running (status: {record['status']}).")
    _touch(vm_id)
    command = f"cd {params.working_dir} && {params.command}" if params.working_dir else params.command
    start = time.time()
    result = await _ssh_exec(vm_id, command, timeout=params.timeout)
    return {"vm_id": vm_id, "command": params.command, **result,
            "elapsed_seconds": round(time.time() - start, 2)}
```

This endpoint is admin-only by network position (same as the rest of the node-agent
REST API); it does not auto-create/resume — it requires the VM to exist and be
running, returning 404/409 otherwise. Documented in `CLAUDE.md`'s REST table.

## Output & UX

- **Default:** human-readable `lipgloss` tables (and a summary line for `ls`/`top`,
  matching the current `fc-top` header). Color by status (running=green, paused=cyan,
  error=red), same palette as `fc-top.py`.
- **`-o json`** on read commands (`ls`, `get`) emits the raw aggregated JSON for
  scripting; write commands print a one-line result (and the exec stdout/stderr).
- **Exit codes:** `0` success; non-zero on any failed REST call, kubectl error, or
  partial failure in a bulk op (with a per-item summary printed).

### Safety on destructive commands
`destroy`, `reset`, and `drain` print exactly what they will affect (e.g. "About to
destroy 8 VMs across 3 nodes and delete 5 Session CRs") and require interactive
confirmation. `-y`/`--yes` skips the prompt for scripted use. `reset` reports a
per-VM DELETE result and the session-deletion result, then re-verifies empty (the
behavior `cleanup_fresh.py` proved out).

## `fcctl top`

A `bubbletea` program: a `Model` holding the last gather, a `tea.Tick` every
`--refresh` seconds (default 2s) that fires a **`tea.Cmd`** running the same gather
used by `ls` (kubectl CRs + parallel `/vms`) off the UI goroutine and delivering it
back as a message — gather must never run inside `Update`/`View`, or the slow kubectl
+ HTTP calls would freeze the dashboard. `View` renders the summary panel + NodeAgents +
Sessions + VMs tables via `render.go`. `q`/`ctrl-c` quits; `r` forces an immediate
refresh. Unreachable node-agents render a red `down` in the REACH column (same as
today). This subsumes `fc-top.py`.

`fc-top.py` is retired; if a `fc-top` command is still wanted on hosts, ship a
one-line wrapper that `exec`s `fcctl top "$@"`.

## Configuration

Flags override env override defaults:
- `--namespace` / `FC_MCP_NAMESPACE` (default `fc-mcp`)
- `--node-port` / `FC_MCP_NODE_PORT` (default `8080`)
- `--lease` / `FC_MCP_LEASE` (default `fc-mcp-router-leader`)
- `--refresh` (top only, default `2`)
- `-o/--output` (`table`|`json`, default `table`)
- `-y/--yes`
- `--kubeconfig` / `KUBECONFIG` (default `~/.kube/config`) — resolved once at startup
  and **always** passed explicitly as `kubectl --kubeconfig <path> …` on every
  invocation, never left to ambient discovery. This keeps behavior deterministic
  across users and `sudo` (where `HOME`, and therefore the default kubeconfig, differ).
  `fcctl` errors early with a clear message if the resolved path does not exist.
- `kubectl` binary discovered on `PATH` (override `KUBECTL`).

## Build & distribution

Cross-compile from any dev machine; no Go toolchain needed on the cluster host:
```bash
cd fcctl
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -o ../bin/fcctl .
scp ../bin/fcctl ec2-user@<host>:~/fcctl
```
A `fcctl/Makefile` provides `make build` (host arch) and `make linux` (the
cross-compile above). `CGO_ENABLED=0` yields a static binary that runs on the AL2
host without glibc concerns.

## Error handling

- **kubectl failure** (no cluster, RBAC): surface the last stderr line; `ls`/`top`
  degrade by showing the kubectl error in-band rather than crashing (as `fc-top.py`
  does), other commands exit non-zero.
- **Node-agent unreachable**: per-node timeout (default 5s); `ls`/`top` mark it
  `down` and continue; targeted commands error clearly naming the node.
- **vm_id not found** in the index: clear "no such VM <id> on any node" error.
- **Bulk ops**: never abort on the first failure — collect per-item results, print a
  summary, exit non-zero if any failed.

## Testing

- **Go unit tests** for: CR JSON unmarshalling (fixture JSON from `kubectl`),
  `/vms` response parsing, the `vm_id → node` index builder, node selection for
  `create`, and table rendering (golden strings). These need no cluster — feed
  recorded JSON.
- **`net/http/httptest`** fake node-agent to test the REST client methods and the
  bulk `reset` flow (including a node returning an error).
- **Server change**: extend `tests/test_phase0.py` style — assert the new endpoint
  404s a missing VM and 409s a non-running one (logic-level; full SSH path still
  needs Linux+KVM).
- **Manual end-to-end** on the KVM host: `ls`, `create`, `exec` (marker file),
  `pause`/`resume`, `top`, and `reset` against the live kind cluster.

## Critical files
- `fcctl/` (new Go module: `main.go`, `kube.go`, `client.go`, `index.go`,
  `render.go`, `tui.go`, `cmd_*.go`, `Makefile`, `go.mod`)
- `server.py` — add `VmExecInput` + `POST /vms/{vm_id}/exec`
- `CLAUDE.md` — document `fcctl` and the new endpoint in the REST table
- `fc-top.py` — retired (optional thin `fc-top` → `fcctl top` shim)
- `cleanup_fresh.py` — retired (superseded by `fcctl reset`)
