# Firecracker Bash MCP

A remote bash execution MCP server where each session runs inside a **Firecracker microVM** — giving you full root access, persistent disk state, and native pause/resume with full memory snapshot support.

## Architecture

```
Claude / MCP Client
        │
        │  HTTP (MCP protocol)
        ▼
┌──────────────────────┐
│  FastMCP Server      │   Python, port 8080
│  (server.py)         │
└──────────┬───────────┘
           │ Unix socket API
           ▼
┌──────────────────────┐    ┌─────────────────────┐
│  Firecracker VM 1    │    │  Firecracker VM 2   │
│  172.16.0.2          │    │  172.16.0.3         │
│  SSH port 50000      │    │  SSH port 50001     │
│  overlay-<id>.ext4   │    │  overlay-<id>.ext4  │
└──────────────────────┘    └─────────────────────┘
           │
     tap device
           │
    fc-br0 bridge
           │
      host NAT → internet
```

### Why Firecracker?

- **Real VM isolation** — each session is a full Linux kernel, not a container
- **Native snapshot API** — pause/resume is a first-class Firecracker feature
  - `CreateSnapshot` writes memory state + disk state to files
  - `LoadSnapshot` restores it exactly, including all running processes
- **Fast boot** — Firecracker VMs boot in ~125ms
- **Low overhead** — ~5MB overhead per VM vs ~100MB+ for QEMU
- **Root access inside** — full package install, kernel modules, etc.

### Pause/Resume Mechanism

```
RUNNING VM
    │
    │  vm_pause
    ▼
Firecracker API: PATCH /vm {"state": "Paused"}   ← freeze guest
Firecracker API: PUT /snapshot/create             ← dump memory + vmstate to disk
Kill FC process
    │
    ▼
STATE: paused
    ├── snapshots/<vm_id>/<snap_id>/memory.bin    ← full RAM dump
    └── snapshots/<vm_id>/<snap_id>/vmstate.bin   ← CPU + device state
    + overlays/<vm_id>.ext4                        ← disk (already on disk)

    │
    │  vm_resume
    ▼
Start new Firecracker process
Firecracker API: PUT /snapshot/load               ← restore memory + vmstate
    │
    ▼
VM resumes exactly where it left off
All processes running, open files intact, memory intact
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `vm_create` | Create and boot a new microVM |
| `bash_exec` | Run a command as root in a VM |
| `vm_pause` | Snapshot VM (memory + disk) and stop it |
| `vm_resume` | Restore VM from snapshot, resume all processes |
| `vm_list` | List all VMs and their status |
| `vm_status` | Detailed status + snapshot history for one VM |
| `vm_destroy` | Permanently delete VM and all data |

## Setup

### Prerequisites

- Linux host with KVM support (`ls /dev/kvm`)
- `firecracker` and `jailer` binaries ([releases](https://github.com/firecracker-microvm/firecracker/releases))
- `debootstrap` and `e2fsprogs` for rootfs build
- Python 3.11+

### 1. Build the VM images

```bash
# Download pre-built kernel (fastest)
sudo FC_BASE_DIR=/opt/fc-mcp bash scripts/build-kernel.sh

# Build Ubuntu 22.04 rootfs (~5 minutes)
sudo FC_BASE_DIR=/opt/fc-mcp bash scripts/build-rootfs.sh
```

### 2. Set up host networking

```bash
sudo bash scripts/setup-network.sh
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 4. Start the MCP server

```bash
bash scripts/start.sh
# or
FC_BASE_DIR=/opt/fc-mcp python3 src/server.py --port 8080
```

### Docker

```bash
docker build -t fc-bash-mcp .
docker run --privileged \
  --device /dev/kvm \
  -v /opt/fc-mcp:/opt/fc-mcp \
  -p 8080:8080 \
  fc-bash-mcp
```

### Kubernetes

The deployment requires a node with KVM access (bare metal or nested virt):

```bash
# Label a node
kubectl label node my-metal-node fc-mcp=true

# Deploy
kubectl apply -f kubernetes/
```

## Connecting to Claude Code

Add to your MCP config (`~/.claude/mcp_config.json` or `claude mcp add`):

```json
{
  "mcpServers": {
    "firecracker-bash": {
      "type": "http",
      "url": "http://your-host:8080/mcp"
    }
  }
}
```

Or via CLI:
```bash
claude mcp add --type http firecracker-bash http://your-host:8080/mcp
```

## Example Session

```
# Create a VM
vm_create(name="dev-env", vcpu=2, mem_mb=1024)
→ {"vm_id": "abc123...", "status": "running", "ssh_port": 50000}

# Install something
bash_exec(vm_id="abc123...", command="apt-get install -y python3-pip")

# Do some work
bash_exec(vm_id="abc123...", command="pip install numpy && python3 -c 'import numpy; print(numpy.__version__)'")

# Pause with full state preserved
vm_pause(vm_id="abc123...")
→ {"snapshot_id": "snap001", "mem_snapshot_mb": 512.3, "status": "paused"}

# ... hours/days later ...

# Resume — numpy is still installed, any running processes restored
vm_resume(vm_id="abc123...")
→ {"status": "running", "restored_from_snapshot": "snap001"}
```

## Security Notes

- The MCP server runs as root on the host (required for KVM + tap networking)
- VMs are isolated from each other and the host via VM boundary
- Expose the MCP server only on trusted networks or behind auth middleware
- SSH keys are auto-generated at `$FC_BASE_DIR/vm_ssh_key`
- For production: add mTLS or an auth token middleware in front of the MCP HTTP server

## Limitations

- **Single-host**: VMs are tied to the host they were created on (snapshots are local files)
- **KVM required**: No hardware virt = no Firecracker. Not possible in most cloud VMs unless nested virt is enabled or you use metal instances.
- **Snapshot restore is same-host only**: You can't move a snapshot to another host without also moving the overlay disk
- **Network persistence across pause**: The guest IP/MAC is preserved in the snapshot; host tap devices need to be re-created on resume (the start script handles this)
