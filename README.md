# Firecracker Bash MCP

A remote bash execution server where each session runs inside a **Firecracker microVM** — giving you full root access, persistent disk state, and native pause/resume with full memory snapshot support.

`server.py` is a combined **FastAPI + FastMCP** app on a single port (8080):

- **MCP tool** `bash_exec` — mounted at `/mcp`. The only MCP tool.
- **REST API** for VM lifecycle (create/list/status/pause/resume/destroy) — mounted at `/vms/*`.
- **Swagger UI** — `/docs`.

## Architecture

```
Claude / MCP Client (any machine)
        │  HTTP streamable-http, port 8080
        ▼
┌──────────────────────────────────────┐
│  server.py (FastAPI + FastMCP)        │   Linux host, runs as root
│  /mcp → bash_exec   /vms/* → REST     │
└──────────┬───────────────────────────┘
           │ Firecracker API over Unix socket (sockets/<vm_id>.sock)
           ▼
┌──────────────────────┐    ┌─────────────────────┐
│  Firecracker VM 1    │    │  Firecracker VM 2   │
│  172.16.0.2          │    │  172.16.0.3         │
│  overlay-<id>.ext4   │    │  overlay-<id>.ext4  │
└──────────┬───────────┘    └──────────┬──────────┘
           │ tap device → fc-br0 bridge → host NAT → internet
           ▼
The server SSHes into each VM directly at 172.16.0.X:22 over the bridge.
```

### Why Firecracker?

- **Real VM isolation** — each session is a full Linux kernel, not a container
- **Native snapshot API** — pause/resume is a first-class Firecracker feature
  - `CreateSnapshot` writes memory state + vmstate to files
  - `LoadSnapshot` restores it exactly, including all running processes
- **Fast boot** — Firecracker VMs boot in ~125ms
- **Low overhead** — ~5MB overhead per VM vs ~100MB+ for QEMU
- **Root access inside** — full package install, kernel modules, etc.

### Pause/Resume Mechanism

```
RUNNING VM
    │  POST /vms/<vm_id>/pause
    ▼
Firecracker API: PATCH /vm {"state": "Paused"}   ← freeze guest
Firecracker API: PUT /snapshot/create             ← dump memory + vmstate to disk
Kill the Firecracker process
    │
    ▼
STATE: paused
    ├── snapshots/<vm_id>/memory.bin    ← full RAM dump
    └── snapshots/<vm_id>/vmstate.bin   ← CPU + device state
    + overlays/<vm_id>.ext4              ← disk (already on disk)
    │
    │  POST /vms/<vm_id>/resume   (or the next session bash_exec auto-resumes)
    ▼
Start a fresh Firecracker process
Firecracker API: PUT /snapshot/load               ← restore memory + vmstate
    │
    ▼
VM resumes exactly where it left off — all processes running, memory intact
```

There is exactly **one snapshot per VM**, overwritten on each pause (no snapshot history).

## MCP Tool

Only one tool is exposed over MCP. VM lifecycle is handled by the REST API (below).

| Tool | Description |
|------|-------------|
| `bash_exec` | Run a shell command as root inside a VM. Params: `command`, optional `vm_id`, `working_dir`, `timeout` (1–600s). Returns JSON with `stdout`, `stderr`, `returncode`, `elapsed_seconds`. |

**Session-managed VMs:** if you call `bash_exec` **without** a `vm_id`, the server creates a VM bound to your MCP session on the first call and reuses it for the rest of the session — auto-resuming it if it was paused. Pass an explicit `vm_id` to target a specific VM instead.

## Setup

**Requires Linux + KVM.** Firecracker does not run on macOS (you can develop the server there, but VMs only boot on Linux).

### Fastest path

One idempotent script does everything — installs deps, `uv`, Firecracker, runs `uv sync`, builds the kernel + rootfs, and configures host networking:

```bash
sudo FC_BASE_DIR=/opt/fc-mcp bash scripts/setup-firecracker.sh
```

> All operational shell scripts live in `scripts/`.

### Manual setup

#### Prerequisites

- Linux host with KVM support (`ls /dev/kvm`)
- `firecracker` binary ([releases](https://github.com/firecracker-microvm/firecracker/releases))
- `debootstrap` + `e2fsprogs` for the rootfs build, `iproute2` + `iptables` for networking
- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)

#### 1. Build the VM images

```bash
# Download the pre-built Firecracker kernel (~5.10.225)
sudo FC_BASE_DIR=/opt/fc-mcp bash scripts/build-kernel.sh

# Build the Ubuntu 22.04 rootfs (~5 minutes)
sudo FC_BASE_DIR=/opt/fc-mcp bash scripts/build-rootfs.sh
```

#### 2. Set up host networking (bridge + NAT)

```bash
sudo bash scripts/setup-network.sh
```

#### 3. Install Python dependencies

```bash
uv sync
```

#### 4. Start the server

```bash
# Via the start script (generates the SSH key + runs preflight checks)
sudo FC_BASE_DIR=/opt/fc-mcp bash scripts/start.sh

# Or directly
sudo FC_BASE_DIR=/opt/fc-mcp uv run python server.py --port 8080
```

### Docker

Requires `--privileged` and `/dev/kvm`:

```bash
docker build -t fc-bash-mcp .
docker run --privileged \
  --device /dev/kvm \
  -v /opt/fc-mcp:/opt/fc-mcp \
  -p 8080:8080 \
  fc-bash-mcp
```

### Kubernetes (HA)

The current topology is a leader-elected **router** plus a node-agent **StatefulSet** (one pod per
KVM-capable node), with routing state in `Session`/`NodeAgent` CRDs. Label the node pool, then
apply the CRDs followed by the manifests:

```bash
# Label the KVM-capable node pool
kubectl label node <kvm-node> fc-mcp=true

# CRDs first, then the router + node-agent StatefulSet + services/RBAC
kubectl apply -f deploy/crds/
kubectl apply -f kubernetes/
```

Point Claude Code at the **router** Service (not a node). The node-agents run `privileged` with
`hostNetwork: true` (tap-device visibility) and an initContainer that runs `setup-network.sh`;
VMs are pinned to their node. For local end-to-end validation on a single KVM host,
`kind/kind-up.sh` stands up the whole stack in a 3-worker kind cluster.

> The earlier single-host manifest is retired to `archive/deployment.yaml` — superseded by this
> StatefulSet topology and kept only for reference.

## Connecting Claude Code

Point Claude Code at the host running the server:

```bash
# Remote host
claude mcp add --transport http firecracker-bash http://<host>:8080/mcp

# Local (Claude Code on the same Linux host)
claude mcp add --transport http firecracker-bash http://localhost:8080/mcp
```

The MCP connection gives Claude Code the `bash_exec` tool. VM lifecycle (create/pause/resume/destroy) is done via the REST API or the Swagger UI at `http://<host>:8080/docs`.

## REST API

Interactive docs at `http://<host>:8080/docs`.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/vms` | Create and boot a VM (copies base rootfs → overlay, launches Firecracker, waits for SSH). Returns a `vm_id`. |
| `GET` | `/vms` | List all VMs. |
| `GET` | `/vms/{vm_id}` | VM status + current snapshot info. |
| `POST` | `/vms/{vm_id}/pause` | Freeze the VM, write its snapshot, kill the Firecracker process. |
| `POST` | `/vms/{vm_id}/resume` | Start a fresh Firecracker process and load the VM's snapshot. |
| `DELETE` | `/vms/{vm_id}` | Kill the VM and delete its overlay, snapshots, socket, and state. |

VMs default to 2 vCPU / 512 MB RAM / 2048 MB disk; `POST /vms` accepts `name`, `vcpu`, `mem_mb`, and `disk_mb` overrides.

## Example Session

The simplest flow doesn't touch the REST API at all — `bash_exec` manages a VM for your session:

```
# First call auto-creates the session's VM
bash_exec(command="apt-get update && apt-get install -y python3-pip")
→ {"vm_id": "abc123...", "stdout": "...", "returncode": 0, "elapsed_seconds": 12.4}

# Subsequent calls reuse the same VM
bash_exec(command="pip install numpy && python3 -c 'import numpy; print(numpy.__version__)'")
→ {"vm_id": "abc123...", "stdout": "2.1.0\n", "returncode": 0, ...}

# Pause with full state preserved (REST, using the vm_id from above)
POST /vms/abc123.../pause
→ {"vm_id": "abc123...", "status": "paused", "mem_snapshot_mb": 512.0}

# ... hours/days later — the next bash_exec on the same session auto-resumes it,
#     or resume explicitly:
POST /vms/abc123.../resume
→ {"vm_id": "abc123...", "status": "running", "ssh_ready": true}
# numpy is still installed; any running processes are restored.
```

## Security Notes

- The server runs as root on the host (required for KVM + tap networking)
- VMs are isolated from each other and the host by the VM boundary
- Expose the server only on trusted networks or behind auth middleware
- SSH into VMs uses an auto-generated ed25519 key at `$FC_BASE_DIR/vm_ssh_key`
- For production: add mTLS or an auth-token middleware in front of the HTTP server

## Limitations

- **Single-host**: VMs are tied to the host they were created on (snapshots are local files)
- **KVM required**: no hardware virt = no Firecracker. Most cloud VMs need nested virt enabled or a metal instance.
- **Snapshot restore is same-host only**: moving a snapshot to another host means moving the overlay disk too
- **Tap-device cap**: `setup-network.sh` pre-creates 32 tap devices, so 32 concurrent VMs is the ceiling without re-running it
- **Session binding is in-memory**: the session→VM mapping resets on server restart (VMs themselves persist via `vm-state.json`)
