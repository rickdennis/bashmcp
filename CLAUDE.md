# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A combined **FastAPI + FastMCP server** (`server.py`). VM lifecycle (create/list/status/pause/resume/destroy) is a REST API at `/vms/*`. Only `bash_exec` is exposed as an MCP tool, mounted at `/mcp`. Both share port 8080. Each VM has its own overlay disk, and full memory+disk snapshots enable true pause/resume. The server runs on the host as root and SSHes into each VM directly over the bridge (`172.16.0.X:22`).

**One VM per MCP session (the primary flow):** `bash_exec` defaults to auto-managing a VM. If called without a `vm_id`, it looks up the VM bound to the current MCP session, creating one on first call and auto-resuming it if it was paused. Most Claude Code usage never touches the REST API — it just calls `bash_exec` and a VM appears. Passing an explicit `vm_id` bypasses session resolution. See `_resolve_session_vm()` and the `SessionVMMap` class.

**Requires Linux + KVM** — Firecracker does not run on macOS. Development of the server itself is fine on macOS but VMs can only be created on Linux.

**Two deployment modes:**
- **Standalone (single host):** run `server.py` directly; its MCP mount at `/mcp` serves `bash_exec` and manages VMs locally. This is the dev/simple path described below.
- **HA (multi-node Kubernetes):** a leader-elected **router** (`proxy/`) is the single MCP endpoint; it places each session on a node and forwards execution to that node's `server.py` (now a **node-agent**) over an internal `POST /exec`. State lives in `Session`/`NodeAgent` CRDs (`fcmcp.io/v1alpha1`). VMs are **pinned** to their node (node loss = that node's VMs lost; survivors unaffected). See `kubernetes/`, `deploy/crds/`, and `proxy/`.

## Linux Host Setup (One-Time)

**Fastest path:** `sudo FC_BASE_DIR=/opt/fc-mcp bash scripts/setup-firecracker.sh` runs all six steps below idempotently (deps → uv → Firecracker → `uv sync` → build images → networking). The manual steps are below for reference. (All operational shell scripts live in `scripts/`.)

```bash
# 1. Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Install Firecracker
FC_VERSION=v1.10.1
ARCH=$(uname -m)
curl -fsSL "https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-${ARCH}.tgz" \
  | tar -xz --strip-components=1
sudo mv release-${FC_VERSION}-${ARCH}/firecracker-${FC_VERSION}-${ARCH} /usr/bin/firecracker
sudo chmod +x /usr/bin/firecracker

# 3. Install system deps
sudo apt-get install -y openssh-client e2fsprogs iproute2 iptables

# 4. Clone repo and install Python deps
git clone <repo> && cd bashmcp
uv sync

# 5. Build VM images (must do in order, ~5-10 min total)
sudo FC_BASE_DIR=/opt/fc-mcp bash scripts/build-kernel.sh    # downloads pre-built kernel (~200MB)
sudo FC_BASE_DIR=/opt/fc-mcp bash scripts/build-rootfs.sh    # bootstraps Ubuntu 22.04 rootfs

# 6. Set up host networking (bridge + NAT for VMs)
sudo bash scripts/setup-network.sh
```

## Running the Server

```bash
# Direct (local dev, data in ./data)
FC_BASE_DIR=./data uv run python server.py --port 8080

# Production (data in /opt/fc-mcp, must run as root for Firecracker)
sudo FC_BASE_DIR=/opt/fc-mcp uv run python server.py --port 8080

# Via start script (does preflight checks first)
sudo FC_BASE_DIR=/opt/fc-mcp bash scripts/start.sh

# Docker (requires --privileged and /dev/kvm)
docker build -t fc-bash-mcp .
docker run --privileged --device /dev/kvm -v /opt/fc-mcp:/opt/fc-mcp -p 8080:8080 fc-bash-mcp
```

## Connecting Claude Code to the Server

Run this once on any machine that has Claude Code installed, pointing at the Linux host:

```bash
claude mcp add --transport http firecracker-bash http://<linux-host-ip>:8080/mcp
```

For local use (Claude Code running on the same Linux host):
```bash
claude mcp add --transport http firecracker-bash http://localhost:8080/mcp
```

The MCP connection gives Claude Code access to `bash_exec`. VM management (create/pause/resume/destroy) is done via the REST API or the Swagger UI at `http://<host>:8080/docs`.

## Workflow

**Default (session-managed):**
1. **Start the server** on the Linux host (see above)
2. **Tell Claude Code** to run commands: *"Run `apt-get install -y curl`"* — `bash_exec` auto-creates the session's VM on the first call and reuses it thereafter.
3. **Pause when done** to free host RAM: `POST /vms/<vm_id>/pause`. The next `bash_exec` on that session auto-resumes it.

**Manual (explicit VM):** Create a VM via `POST /vms` (or the Swagger UI), then pass its `vm_id` to `bash_exec` to target it directly, bypassing session resolution. Use this when you need multiple named VMs or want lifecycle control.

## Architecture

```
Claude Code (any machine)
    │  HTTP streamable-http, port 8080
    ▼
server.py on Linux host (FastAPI + FastMCP, asyncio)
    │  Unix socket API  (FC_BASE_DIR/sockets/<vm_id>.sock)
    ▼
Firecracker process per VM
    │  tap device (fc-tap-NNNNNNNN) → fc-br0 bridge → host NAT
    ▼
VM gets 172.16.0.X; server SSHes to 172.16.0.X:22 directly over the bridge
```

The server connects straight to the VM's bridge IP on port 22 (the old unused `ssh_port` field has been removed). `setup-network.sh` pre-creates 32 tap devices, so **32 concurrent VMs is the hard cap per node** without re-running it. IP/tap slots are assigned by `SlotAllocator` (a persisted free-list in `slots.json`), not by list position.

**Key files on host (`FC_BASE_DIR`):**
- `vm-images/vmlinux-5.10` — shared kernel (read-only)
- `vm-images/ubuntu-22.04-base.ext4` — base rootfs (read-only)
- `overlays/<vm_id>.ext4` — per-VM copy-on-write disk
- `snapshots/<vm_id>/memory.bin` + `vmstate.bin` — single pause snapshot per VM (overwritten on each pause; no history)
- `sockets/<vm_id>.sock` + `<vm_id>-vsock.sock` — Firecracker management API + vsock sockets
- `vm-state.json` — VM registry, JSON-persisted (`VMState` class)
- `session-vm.json` — session-id → vm_id mapping (`SessionVMMap` class)
- `slots.json` — persisted IP/tap slot free-list (`SlotAllocator` class)
- `vm_ssh_key` — auto-generated ed25519 key for SSH into VMs

## Core Patterns

**VM state machine:** `creating → booting → running ↔ paused | error | destroyed`

**Session → VM resolution:** `_resolve_session_vm(session_id)` maps a session id to a VM, creating or auto-resuming as needed. In **HA mode** the router passes the stable `Mcp-Session-Id` (which the SDK assigns and the client echoes), so the binding is durable and routable. In **standalone mode** the legacy `bash_exec` tool still keys on `str(id(ctx.request_context.session))` (in-process, lost on restart). A paused VM is auto-resumed inline (same snapshot-load path as the REST resume endpoint).

**Startup reconcile:** `reconcile_on_startup()` runs before serving — a process/pod restart kills child FC PIDs but leaves overlays/snapshots on the (node-local) disk. It re-adopts genuinely live VMs (`os.kill` + `/proc/<pid>/comm`), down-converts dead-but-snapshotted VMs to `paused` (so the next call auto-resumes), errors the rest, and rebuilds the slot free-list from survivors. `/ready` stays 503 until it completes.

**Pause/resume:** Uses Firecracker's native snapshot API — PATCH `/vm` to freeze, PUT `/snapshot/create` to dump memory+vmstate to disk, then SIGKILL the FC process. Resume launches a fresh FC process (after unlinking the stale sockets) and calls PUT `/snapshot/load`. There is exactly one snapshot per VM, overwritten on each pause. `_pause_vm(vm_id, archive)` is the shared core (the REST pause endpoint and `idle_pause_loop` archive=True; `api_drain`/preStop archive=False).

**S3 snapshot archival (optional, `FC_S3_BUCKET`):** to make paused VMs survive *node* loss (local PV is node-local), every archive=True pause uploads `overlay.ext4`+`memory.bin`+`vmstate.bin`+`meta.json` to `s3://<bucket>/<FC_S3_PREFIX>/<vm_id>/` (`_s3_archive_vm`, boto3 via `asyncio.to_thread`). Resume and `POST /restore` pull them back when the local files are gone (`_s3_restore_vm`), reconstruct the record from `meta.json`, and **reserve the same slot** so the snapshot's tap/IP/MAC match. Disabled when `FC_S3_BUCKET` is unset; creds via the boto3 default chain (EC2 instance role through IMDS, or IRSA on EKS — `s3:Get/Put/Delete/ListBucket`). Running (un-paused) VMs are still lost on node death — accepted. Router-driven auto-failover (re-place `Lost` sessions → `/restore`) is a deferred Phase B.

**Command execution:** All `bash_exec` calls SSH as root directly to the VM's bridge IP (`172.16.0.X:22`) using `vm_ssh_key`, with host-key checking disabled. The `working_dir` param prepends `cd <dir> &&` to the command string.

**IP/MAC/tap assignment:** A VM's **slot** (index 2–33) is allocated once by `SlotAllocator` and stored on the record, giving IP `172.16.0.{slot}`, tap `fc-tap-{(slot-2):08x}`, and MAC from `_gen_mac`. Slots are allocated under `_create_lock`, released on destroy, and rebuilt from survivors by reconcile. This replaced the old positional indexing (`len(list_all())+2`), which drifted a survivor's tap/IP whenever an earlier VM was destroyed. Don't hand-edit `vm-state.json`/`slots.json`.

**Dependencies managed with uv** — run `uv sync` to install. `uv run python server.py` to execute.

## MCP Tool

| Tool | Notes |
|------|-------|
| `bash_exec` | SSH exec into VM as root. `vm_id` optional (omit → session-managed VM); also `command`, `working_dir`, `timeout` (1–600s). Returns JSON: `stdout`, `stderr`, `returncode`, `elapsed_seconds`. |

## REST API (`/vms`)

Interactive docs at `http://<host>:8080/docs`

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/health` | Liveness — always 200 while serving |
| `GET` | `/ready` | Readiness — 503 until reconcile done / images+key present / bridge up / a free slot exists |
| `POST` | `/exec` | **Router-internal.** `{session_id, command, working_dir, timeout}` → resolve/create/resume the session's VM and run it |
| `POST` | `/drain` | **preStop hook.** Pause+snapshot every running VM so a restart/upgrade preserves in-VM state |
| `POST` | `/restore` | **Router-internal.** `{vm_id, session_id?}` → pull a VM's snapshot from S3, resume it, bind the session (recover after node loss; needs `FC_S3_BUCKET`) |
| `POST` | `/vms` | Create VM — allocates a slot, copies base rootfs → overlay, launches FC, waits for SSH (45s) |
| `GET` | `/vms` | List all VMs from in-memory state |
| `GET` | `/vms/{vm_id}` | VM status + current snapshot info (single snapshot, not a history) |
| `POST` | `/vms/{vm_id}/exec` | **Admin (fcctl).** `{command, working_dir?, timeout?}` → run a command in a *specific* VM. No session resolution / no auto-create; a paused VM is auto-resumed first: 404 if unknown, 409 if otherwise not running |
| `POST` | `/vms/{vm_id}/pause` | Freeze VM, write snapshot, kill FC process |
| `POST` | `/vms/{vm_id}/resume` | Start fresh FC, load the VM's snapshot (no request body) |
| `DELETE` | `/vms/{vm_id}` | SIGKILL + delete overlay, snapshots, socket, release slot, state entry, session mapping |

## Admin CLI (`fcctl`)

`fcctl/` is a Go (cobra) admin/control-plane CLI for the HA cluster — the durable replacement for `fc-top.py` and `cleanup_fresh.py`. It is **not** the data plane: clients still talk MCP to the router; `fcctl` is for operators. It reaches the cluster two ways, mirroring `fc-top.py`: **CRs via `kubectl`** (`nodeagents`/`sessions`/the router `lease`, always with an explicit `--kubeconfig`) and **per-VM ops via direct REST to each node-agent's `podIP:8080`** (no router dependency). Commands: `ls vms|sessions|nodes`, `get vm|session|node`, `create`, `exec <id> -- <cmd>`, `pause`, `resume`, `restore`, `destroy`, `drain <node>`, `reset`, and `top` (a `bubbletea` dashboard that subsumes `fc-top.py`). `exec` is what `POST /vms/{vm_id}/exec` exists for. `destroy`/`reset`/`drain` confirm interactively unless `-y`. Read commands take `-o json`.

```bash
cd fcctl && make linux           # static linux/amd64 binary -> ../bin/fcctl
scp ../bin/fcctl <host>:~/fcctl   # cluster hosts are linux/amd64
# On a host with kubectl + kubeconfig + reachability to node podIPs:
fcctl ls nodes && fcctl create --name demo && fcctl exec <id> -- uname -a
```

Tests are pure Go (`cd fcctl && go test ./...`): CR/`/vms` parsing, the `vm_id→node` index, node selection, render golden strings, and an `httptest` fake node-agent for the REST client + bulk reset. **Gotcha:** when you rebuild the node-agent image, the StatefulSet's `OnDelete` strategy means existing pods keep the *old* code — `kubectl delete pod fc-node-agent-N` to roll each onto the new image before exercising the new endpoint.

## Kubernetes

Two topologies live in the repo:

- **Legacy single-host:** retired to `archive/deployment.yaml` (namespace `fc-mcp`, single replica, `hostNetwork`, `privileged`, `setup-network.sh` initContainer). Superseded by the HA topology below; kept only for reference, not applied or tested.
- **HA (current target):** apply `deploy/crds/` then `kubernetes/`:
  - **Node-agent `StatefulSet`** (`kubernetes/statefulset.yaml`) — one pod per node via hard `podAntiAffinity` (`topologyKey: kubernetes.io/hostname`) on the tainted/labeled `fc-mcp` pool; `hostNetwork`, `privileged`, `/dev/kvm` + `/dev/net/tun`; per-pod **local PV** (`volumeClaimTemplates`, StorageClass `fc-local`, `WaitForFirstConsumer`) so each pod is pinned to its node's disk; `NODE_NAME`/`POD_IP` from the downward API; `OnDelete` update strategy; a **preStop `/drain`** hook (+`terminationGracePeriodSeconds: 120`) that snapshots running VMs before the pod dies.
  - **Router `Deployment`** (`kubernetes/router-deployment.yaml`) — 2 replicas of `proxy.router`; leader-elected via a `coordination.k8s.io` Lease; **only the leader reports `/readyz` Ready**, so the `ClusterIP` Service (`router-service.yaml`) backs the single active replica. Auth/TLS terminate at your org ingress/gateway in front of it.
  - **CRDs** `sessions.fcmcp.io` (session→node binding, routing source of truth) and `nodeagents.fcmcp.io` (per-node capacity/heartbeat; `status.freeTaps` is the capacity authority). RBAC in `kubernetes/rbac.yaml`; PDBs in `kubernetes/pdb.yaml`.
  - **Operational requirement:** annotate the `fc-mcp` pool with `cluster-autoscaler.kubernetes.io/scale-down-disabled=true` — draining a node destroys its local-PV VM state. **Dead-node runbook:** a dead node leaves its StatefulSet ordinal `Pending` on a node-pinned PVC; delete the orphaned PVC + force-delete the pod so it reschedules onto a fresh (empty) node.

`claude mcp add --transport http firecracker-bash http://<router-or-gateway>/mcp` — point Claude Code at the **router** Service (or the gateway in front of it), not a node.

## Local validation in kind (single KVM host)

`kind/kind-up.sh` (run as root on a Linux+KVM host) stands up the whole HA stack in a
3-worker kind cluster and boots real Firecracker VMs inside the kind nodes. Validated
end-to-end on Amazon Linux 2. Hard-won gotchas baked into the scripts:

- **cgroup driver must be `cgroupfs` everywhere.** AL2 is cgroup v1 with old systemd;
  the default systemd driver makes kind node kubelets crash-loop creating `kubepods.slice`.
  `kind-up.sh` pins Docker to `cgroupfs` and `kind/kind-cluster.yaml` patches the kubelet +
  containerd to match.
- **Raise inotify limits** before `kind create` or worker joins fail (`error uploading crisocket`).
- **VM images + SSH key are seeded per node** to `/opt/fc-seed` (hostPath-mounted read-only into
  pods) because each pod's local PV starts empty — the node-agent needs the kernel/rootfs and the
  key that matches the rootfs's baked `authorized_keys`.
- **`ssh -i <key>` reads `<key>.pub` to choose which key to offer** — a stale `.pub` in the data
  dir makes it offer the wrong key. `start.sh` now always derives `.pub` from the private key.
- `kind/mcp_smoke.py` drives `bash_exec` through the router end-to-end; `kind/fc-sshd-debug.sh`
  captures a guest's `sshd -ddd` auth verdict when debugging in-VM SSH.

## Gotchas / Known Gaps

- **Snapshot-resumed VMs busy-loop a vCPU (~100% host CPU).** A VM resumed via
  `snapshot/load` spins its vCPU even when the guest is fully idle (guest reports
  100% idle, tickless, `tsc`, default `hlt` idle, no `idle=poll`) — a Firecracker
  resume behavior (likely a restored LAPIC tsc-deadline timer firing repeatedly),
  not our code. Freshly-booted VMs idle correctly at 0%. **Pausing the VM kills its
  FC process → 0 CPU**, so the node-agent **idle-pause loop** (`idle_pause_loop`,
  `FC_IDLE_PAUSE_SECONDS` default 300s) neutralizes it in steady state — idle VMs
  auto-pause and the next `bash_exec` auto-resumes. A source-level fix likely needs
  a newer Firecracker release — worth testing.

- **Tests:** there's no framework, but `tests/test_phase0.py` is a dependency-free assert script (`uv run python tests/test_phase0.py`) covering slot stability + reconcile. Firecracker itself still needs Linux+KVM to exercise.
- **MCP transport mode is load-bearing:** both `server.py` and `proxy/router.py` pin `FastMCP(stateless_http=False, json_response=False)`. The SDK keeps stateful sessions in an **in-process dict** and 404s any `Mcp-Session-Id` the live process didn't initialize — which is *why* the router (not the node) terminates MCP, so a node-agent restart doesn't 404 sessions. Treat a change to these flags as a design fork.
- **VMs are pets, pinned to a node.** No cross-node migration; a node death loses its VMs by design (survivors/new sessions are unaffected). On a node-agent pod restart, the router stays up and the next `bash_exec` auto-resumes from the preStop snapshot on the local PV.
- **Cluster-only quirks (untested off-cluster):** CR `/status` patches use `application/merge-patch+json`; the router/node-agent k8s code is verified to import and compile but its API calls are exercised only against a real cluster.
