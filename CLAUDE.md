# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A combined **FastAPI + FastMCP server** (`server.py`). VM lifecycle (create/list/status/pause/resume/destroy) is a REST API at `/vms/*`. Only `bash_exec` is exposed as an MCP tool, mounted at `/mcp`. Both share port 8080. Each VM session has its own overlay disk, and full memory+disk snapshots enable true pause/resume. The server runs on the host as root and communicates with VMs via SSH on ports 50000+.

**Requires Linux + KVM** — Firecracker does not run on macOS. Development of the server itself is fine on macOS but VMs can only be created on Linux.

## Linux Host Setup (One-Time)

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
sudo FC_BASE_DIR=/opt/fc-mcp bash build-kernel.sh    # downloads pre-built kernel (~200MB)
sudo FC_BASE_DIR=/opt/fc-mcp bash build-rootfs.sh    # bootstraps Ubuntu 22.04 rootfs

# 6. Set up host networking (bridge + NAT for VMs)
sudo bash setup-network.sh
```

## Running the Server

```bash
# Direct (local dev, data in ./data)
FC_BASE_DIR=./data uv run python server.py --port 8080

# Production (data in /opt/fc-mcp, must run as root for Firecracker)
sudo FC_BASE_DIR=/opt/fc-mcp uv run python server.py --port 8080

# Via start script (does preflight checks first)
sudo FC_BASE_DIR=/opt/fc-mcp bash start.sh

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

1. **Start the server** on the Linux host (see above)
2. **Create a VM** via REST or Swagger UI — returns a `vm_id`
3. **Tell Claude Code** to run commands: *"Run `apt-get install -y curl` in VM `<vm_id>`"*
4. **Pause when done** to save state: `POST /vms/<vm_id>/pause`
5. **Resume later** to restore exact state: `POST /vms/<vm_id>/resume`

## Architecture

```
Claude Code (any machine)
    │  HTTP streamable-http, port 8080
    ▼
server.py on Linux host (FastAPI + FastMCP, asyncio)
    │  Unix socket API  (FC_BASE_DIR/sockets/<vm_id>.sock)
    ▼
Firecracker process per VM
    │  tap device → fc-br0 bridge → host NAT
    ▼
VM gets 172.16.0.X, SSH forwarded to host port 50000+N
```

**Key files on host (`FC_BASE_DIR`):**
- `vm-images/vmlinux-5.10` — shared kernel (read-only)
- `vm-images/ubuntu-22.04-base.ext4` — base rootfs (read-only)
- `overlays/<vm_id>.ext4` — per-VM copy-on-write disk
- `snapshots/<vm_id>/<snap_id>/memory.bin` + `vmstate.bin` — pause state
- `sockets/<vm_id>.sock` — Firecracker management API socket
- `vm-state.json` — in-process registry, JSON-persisted (VMState class)
- `vm_ssh_key` — auto-generated ed25519 key for SSH into VMs

## Core Patterns

**VM state machine:** `creating → booting → running ↔ paused | error | destroyed`

**Pause/resume:** Uses Firecracker's native snapshot API — PATCH `/vm` to freeze, PUT `/snapshot/create` to dump memory+vmstate to disk, then SIGKILL the FC process. Resume launches a fresh FC process and calls PUT `/snapshot/load`.

**Command execution:** All `bash_exec` calls SSH as root into the VM's forwarded port. The `working_dir` param prepends `cd <dir> &&` to the command string.

**IP/port assignment:** VMs get IPs `172.16.0.{2,3,...}` and SSH ports `50000, 50001, ...` based on their position in the state list. This is stable as long as state isn't manually edited.

**Dependencies managed with uv** — run `uv sync` to install. `uv run python server.py` to execute.

## MCP Tool

| Tool | Notes |
|------|-------|
| `bash_exec` | SSH exec into VM; supports `working_dir` and `timeout` (1–600s) |

## REST API (`/vms`)

Interactive docs at `http://<host>:8080/docs`

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/vms` | Create VM — copies base rootfs → overlay, launches FC, waits for SSH (45s timeout) |
| `GET` | `/vms` | List all VMs from in-memory state |
| `GET` | `/vms/{vm_id}` | VM status + snapshot history |
| `POST` | `/vms/{vm_id}/pause` | Freeze VM, write snapshot, kill FC process |
| `POST` | `/vms/{vm_id}/resume` | Start fresh FC, load snapshot; body `{"snapshot_id": null}` defaults to latest |
| `DELETE` | `/vms/{vm_id}` | SIGKILL + delete overlay, snapshots, socket, state entry |

## Kubernetes

`deployment.yaml` deploys to namespace `fc-mcp`. Requires nodes labeled `fc-mcp=true`, `hostNetwork: true` (for tap device visibility), and `privileged: true`. Uses an initContainer to run `setup-network.sh` before the main server starts. Single replica only — VMs are local to the host they run on.
