"""
firecracker_bash_mcp - Remote bash execution MCP server backed by Firecracker microVMs.

Each VM runs as a Firecracker microVM with:
  - Full root access inside the VM
  - Persistent disk state across pause/resume
  - Native Firecracker snapshot API for pause/resume with full memory+disk state
  - SSH for command execution
"""

import asyncio
import json
import os
import subprocess
import time
import uuid
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from mcp.server.fastmcp import FastMCP, Context
from pydantic import BaseModel, Field, ConfigDict

# ─── Constants ────────────────────────────────────────────────────────────────

BASE_DIR = Path(os.environ.get("FC_BASE_DIR", "/opt/fc-mcp"))
VM_IMAGES_DIR = BASE_DIR / "vm-images"
SNAPSHOTS_DIR = BASE_DIR / "snapshots"
SOCKETS_DIR = BASE_DIR / "sockets"
STATE_FILE = BASE_DIR / "vm-state.json"

# Base rootfs image — must be pre-built (see scripts/build-rootfs.sh)
BASE_ROOTFS = VM_IMAGES_DIR / "ubuntu-22.04-base.ext4"
# Kernel image — must be pre-built (see scripts/build-kernel.sh)
KERNEL_IMAGE = VM_IMAGES_DIR / "vmlinux-5.10"

FC_BINARY = os.environ.get("FC_BINARY", "/usr/bin/firecracker")
JAILER_BINARY = os.environ.get("JAILER_BINARY", "/usr/bin/jailer")

DEFAULT_VCPU = 2
DEFAULT_MEM_MB = 512
DEFAULT_DISK_MB = 2048
VM_SSH_START_PORT = 50000  # VMs get SSH on 50000 + index

SSH_KEY_PATH = BASE_DIR / "vm_ssh_key"

log = logging.getLogger("fc_mcp")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ─── VM State Store ────────────────────────────────────────────────────────────

class VMState:
    """In-process VM registry with JSON persistence."""

    def __init__(self):
        self._vms: Dict[str, Dict] = {}
        self._load()

    def _load(self):
        if STATE_FILE.exists():
            try:
                self._vms = json.loads(STATE_FILE.read_text())
            except Exception as e:
                log.warning(f"Could not load state file: {e}")

    def _save(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self._vms, indent=2))

    def create(self, vm_id: str, record: Dict):
        self._vms[vm_id] = record
        self._save()

    def update(self, vm_id: str, fields: Dict):
        if vm_id not in self._vms:
            raise KeyError(f"VM {vm_id} not found")
        self._vms[vm_id].update(fields)
        self._save()

    def get(self, vm_id: str) -> Optional[Dict]:
        return self._vms.get(vm_id)

    def delete(self, vm_id: str):
        self._vms.pop(vm_id, None)
        self._save()

    def list_all(self) -> List[Dict]:
        return list(self._vms.values())


# Global state store
_vm_state = VMState()


# ─── Firecracker API Client ────────────────────────────────────────────────────

class FirecrackerClient:
    """HTTP client for the Firecracker management API (Unix socket)."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        transport = httpx.AsyncHTTPTransport(uds=socket_path)
        self._client = httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            timeout=30.0
        )

    async def put(self, path: str, body: Dict) -> Dict:
        r = await self._client.put(path, json=body)
        r.raise_for_status()
        return r.json() if r.content else {}

    async def patch(self, path: str, body: Dict) -> Dict:
        r = await self._client.patch(path, json=body)
        r.raise_for_status()
        return r.json() if r.content else {}

    async def get(self, path: str) -> Dict:
        r = await self._client.get(path)
        r.raise_for_status()
        return r.json()

    async def close(self):
        await self._client.aclose()


# ─── VM Lifecycle Helpers ──────────────────────────────────────────────────────

def _overlay_path(vm_id: str) -> Path:
    """Each VM gets its own copy-on-write overlay disk."""
    return BASE_DIR / "overlays" / f"{vm_id}.ext4"

def _socket_path(vm_id: str) -> str:
    return str(SOCKETS_DIR / f"{vm_id}.sock")

def _snapshot_dir(vm_id: str) -> Path:
    return SNAPSHOTS_DIR / vm_id

def _ssh_port(vm_id: str) -> int:
    record = _vm_state.get(vm_id)
    if record:
        return record.get("ssh_port", 22222)
    return 22222


async def _create_overlay(vm_id: str, size_mb: int):
    """Create a qcow2-style sparse overlay for the VM's writable disk."""
    overlay = _overlay_path(vm_id)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    # Copy base image (sparse copy to save space)
    proc = await asyncio.create_subprocess_exec(
        "cp", "--sparse=always", str(BASE_ROOTFS), str(overlay),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to create overlay: {stderr.decode()}")
    # Resize to requested size
    proc2 = await asyncio.create_subprocess_exec(
        "resize2fs", str(overlay), f"{size_mb}M",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    await proc2.communicate()


async def _launch_firecracker(vm_id: str, vcpu: int, mem_mb: int, ssh_port: int):
    """Start a Firecracker process and configure the VM via the API."""
    socket = _socket_path(vm_id)
    overlay = _overlay_path(vm_id)
    SOCKETS_DIR.mkdir(parents=True, exist_ok=True)

    # Launch firecracker process (daemonized, API socket ready)
    fc_proc = subprocess.Popen(
        [FC_BINARY, "--api-sock", socket, "--level", "Warning"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for socket to appear
    for _ in range(30):
        if Path(socket).exists():
            break
        await asyncio.sleep(0.2)
    else:
        raise RuntimeError("Firecracker socket never appeared")

    fc = FirecrackerClient(socket)

    # Boot source
    await fc.put("/boot-source", {
        "kernel_image_path": str(KERNEL_IMAGE),
        "boot_args": (
            f"console=ttyS0 reboot=k panic=1 pci=off "
            f"ip=172.16.0.{_vm_index(vm_id)}/24::172.16.0.1:255.255.255.0::eth0:off "
        )
    })

    # Root drive (overlay)
    await fc.put("/drives/rootfs", {
        "drive_id": "rootfs",
        "path_on_host": str(overlay),
        "is_root_device": True,
        "is_read_only": False
    })

    # Machine config
    await fc.put("/machine-config", {
        "vcpu_count": vcpu,
        "mem_size_mib": mem_mb,
        "smt": False
    })

    # Network interface (tap device for SSH)
    tap_name = f"fc-tap-{vm_id[:8]}"
    await fc.put("/network-interfaces/eth0", {
        "iface_id": "eth0",
        "guest_mac": _gen_mac(vm_id),
        "host_dev_name": tap_name
    })

    # vsock for exec (alternative to SSH)
    await fc.put("/vsock", {
        "guest_cid": 3,
        "uds_path": str(SOCKETS_DIR / f"{vm_id}-vsock.sock")
    })

    # Start the VM
    await fc.put("/actions", {"action_type": "InstanceStart"})
    await fc.close()

    return fc_proc.pid


def _vm_index(vm_id: str) -> int:
    """Assign a stable index to VM for IP addressing."""
    vms = _vm_state.list_all()
    for i, v in enumerate(vms):
        if v["vm_id"] == vm_id:
            return i + 2  # Start at .2
    return 2


def _gen_mac(vm_id: str) -> str:
    h = vm_id.replace("-", "")[:10]
    return f"AA:FC:{h[0:2]}:{h[2:4]}:{h[4:6]}:{h[6:8]}"


async def _wait_for_ssh(ssh_port: int, timeout: int = 30) -> bool:
    """Poll until SSH port is responsive inside the VM."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", ssh_port), timeout=2
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            await asyncio.sleep(1)
    return False


async def _ssh_exec(vm_id: str, command: str, timeout: int = 60) -> Dict[str, Any]:
    """Run a command in the VM via SSH and return stdout/stderr/returncode."""
    ssh_port = _ssh_port(vm_id)
    proc = await asyncio.create_subprocess_exec(
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "-i", str(SSH_KEY_PATH),
        "-p", str(ssh_port),
        "root@127.0.0.1",
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "returncode": proc.returncode,
        }
    except asyncio.TimeoutError:
        proc.kill()
        return {"stdout": "", "stderr": "Command timed out", "returncode": -1}


# ─── Pydantic Input Models ─────────────────────────────────────────────────────

class VMCreateInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(..., description="Human-readable label for the VM (e.g. 'my-dev-env')", min_length=1, max_length=64)
    vcpu: int = Field(default=DEFAULT_VCPU, description="Number of vCPUs", ge=1, le=8)
    mem_mb: int = Field(default=DEFAULT_MEM_MB, description="RAM in megabytes", ge=128, le=8192)
    disk_mb: int = Field(default=DEFAULT_DISK_MB, description="Disk size in megabytes", ge=512, le=20480)


class BashExecInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    vm_id: str = Field(..., description="VM ID returned by vm_create")
    command: str = Field(..., description="Shell command to execute (runs as root)", min_length=1, max_length=8192)
    timeout: int = Field(default=60, description="Max seconds to wait for the command", ge=1, le=600)
    working_dir: Optional[str] = Field(default=None, description="Working directory inside the VM (e.g. '/tmp/myproject')")


class VMIDInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    vm_id: str = Field(..., description="VM ID to operate on")


class VMResumeInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    vm_id: str = Field(..., description="VM ID to resume from snapshot")
    snapshot_id: Optional[str] = Field(default=None, description="Snapshot ID to restore (defaults to latest)")


# ─── FastMCP Server ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan():
    """Ensure required directories exist on startup."""
    for d in [BASE_DIR, VM_IMAGES_DIR, SNAPSHOTS_DIR, SOCKETS_DIR,
              BASE_DIR / "overlays"]:
        d.mkdir(parents=True, exist_ok=True)
    log.info(f"Firecracker Bash MCP started. Base dir: {BASE_DIR}")
    yield {}


mcp = FastMCP("firecracker_bash_mcp", lifespan=lifespan)


# ─── Tools ────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="vm_create",
    annotations={
        "title": "Create a new Firecracker microVM",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
)
async def vm_create(params: VMCreateInput, ctx: Context) -> str:
    """Create and boot a new Firecracker microVM.

    Creates a new isolated microVM with its own disk overlay (copy-on-write from the
    base Ubuntu 22.04 image). The VM boots with full root access and SSH enabled.
    Returns a vm_id to use with bash_exec and other tools.

    Args:
        params (VMCreateInput): VM configuration:
            - name (str): Human-readable label
            - vcpu (int): Number of vCPUs (default: 2)
            - mem_mb (int): RAM in MB (default: 512)
            - disk_mb (int): Disk size in MB (default: 2048)

    Returns:
        str: JSON with vm_id, name, status, ssh_port, ip_address
    """
    vm_id = str(uuid.uuid4())
    existing = [v for v in _vm_state.list_all() if v.get("name") == params.name]
    if existing:
        return json.dumps({"error": f"VM named '{params.name}' already exists. Use vm_list to see existing VMs."})

    # Determine SSH port
    port_index = len(_vm_state.list_all())
    ssh_port = VM_SSH_START_PORT + port_index

    record = {
        "vm_id": vm_id,
        "name": params.name,
        "status": "creating",
        "vcpu": params.vcpu,
        "mem_mb": params.mem_mb,
        "disk_mb": params.disk_mb,
        "ssh_port": ssh_port,
        "created_at": time.time(),
        "pid": None,
        "snapshots": [],
    }
    _vm_state.create(vm_id, record)

    await ctx.report_progress(0.1, "Creating disk overlay...")

    try:
        await _create_overlay(vm_id, params.disk_mb)
        await ctx.report_progress(0.4, "Launching Firecracker process...")

        pid = await _launch_firecracker(vm_id, params.vcpu, params.mem_mb, ssh_port)
        _vm_state.update(vm_id, {"pid": pid, "status": "booting"})

        await ctx.report_progress(0.7, "Waiting for SSH to become available...")
        ready = await _wait_for_ssh(ssh_port, timeout=45)

        if not ready:
            _vm_state.update(vm_id, {"status": "error", "error": "SSH timeout"})
            return json.dumps({"error": "VM booted but SSH never became available", "vm_id": vm_id})

        _vm_state.update(vm_id, {"status": "running"})
        await ctx.report_progress(1.0, "VM ready")

        vm_ip = f"172.16.0.{_vm_index(vm_id)}"
        return json.dumps({
            "vm_id": vm_id,
            "name": params.name,
            "status": "running",
            "ssh_port": ssh_port,
            "ip_address": vm_ip,
            "vcpu": params.vcpu,
            "mem_mb": params.mem_mb,
            "disk_mb": params.disk_mb,
            "message": "VM is ready. Use bash_exec to run commands as root."
        }, indent=2)

    except Exception as e:
        _vm_state.update(vm_id, {"status": "error", "error": str(e)})
        return json.dumps({"error": str(e), "vm_id": vm_id})


@mcp.tool(
    name="bash_exec",
    annotations={
        "title": "Execute a bash command in a microVM",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
)
async def bash_exec(params: BashExecInput) -> str:
    """Run a shell command as root inside a running Firecracker microVM.

    Executes an arbitrary bash command inside the specified VM via SSH.
    The command runs as root, so you can install packages, modify system files,
    start/stop services, etc. All state persists on the VM's disk overlay.

    Args:
        params (BashExecInput): Execution parameters:
            - vm_id (str): Target VM ID
            - command (str): Bash command to run (e.g., 'apt-get install -y curl')
            - timeout (int): Max seconds to wait (default: 60, max: 600)
            - working_dir (Optional[str]): Working directory inside VM

    Returns:
        str: JSON with stdout, stderr, returncode, and execution time
    """
    record = _vm_state.get(params.vm_id)
    if not record:
        return json.dumps({"error": f"VM '{params.vm_id}' not found. Use vm_list to see available VMs."})

    if record["status"] != "running":
        return json.dumps({
            "error": f"VM is not running (status: {record['status']}). "
                     f"Use vm_resume if it is paused, or vm_create for a new VM."
        })

    command = params.command
    if params.working_dir:
        command = f"cd {params.working_dir} && {command}"

    start = time.time()
    result = await _ssh_exec(params.vm_id, command, timeout=params.timeout)
    elapsed = round(time.time() - start, 2)

    return json.dumps({
        "vm_id": params.vm_id,
        "command": params.command,
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        "returncode": result["returncode"],
        "elapsed_seconds": elapsed,
    }, indent=2)


@mcp.tool(
    name="vm_pause",
    annotations={
        "title": "Pause and snapshot a microVM",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
)
async def vm_pause(params: VMIDInput, ctx: Context) -> str:
    """Pause a running microVM and save a full memory+disk snapshot.

    Uses Firecracker's native snapshot API to freeze the VM and write both
    the memory state and disk state to disk. The VM's process is then terminated.
    All in-flight state (processes, loaded files, environment variables) is preserved
    and can be restored exactly with vm_resume.

    Args:
        params (VMIDInput): - vm_id (str): ID of the running VM to pause

    Returns:
        str: JSON with snapshot_id, snapshot_path, and size info
    """
    record = _vm_state.get(params.vm_id)
    if not record:
        return json.dumps({"error": f"VM '{params.vm_id}' not found."})
    if record["status"] != "running":
        return json.dumps({"error": f"VM must be running to pause (status: {record['status']})"})

    snapshot_id = str(uuid.uuid4())[:8]
    snap_dir = _snapshot_dir(params.vm_id) / snapshot_id
    snap_dir.mkdir(parents=True, exist_ok=True)

    mem_path = str(snap_dir / "memory.bin")
    state_path = str(snap_dir / "vmstate.bin")

    try:
        socket = _socket_path(params.vm_id)
        fc = FirecrackerClient(socket)

        await ctx.report_progress(0.2, "Pausing VM guest...")
        # Pause the guest
        await fc.patch("/vm", {"state": "Paused"})

        await ctx.report_progress(0.5, "Writing snapshot to disk...")
        # Create snapshot (full — includes memory)
        await fc.put("/snapshot/create", {
            "snapshot_type": "Full",
            "snapshot_path": state_path,
            "mem_file_path": mem_path,
            "version": "1.0.0"
        })

        await fc.close()
        await ctx.report_progress(0.8, "Stopping VM process...")

        # Kill the FC process
        pid = record.get("pid")
        if pid:
            try:
                os.kill(pid, 15)  # SIGTERM
                await asyncio.sleep(1)
                os.kill(pid, 9)   # SIGKILL if needed
            except ProcessLookupError:
                pass

        snapshot_entry = {
            "snapshot_id": snapshot_id,
            "created_at": time.time(),
            "mem_path": mem_path,
            "state_path": state_path,
        }
        snapshots = record.get("snapshots", []) + [snapshot_entry]
        _vm_state.update(params.vm_id, {
            "status": "paused",
            "pid": None,
            "latest_snapshot": snapshot_id,
            "snapshots": snapshots,
        })

        mem_size_mb = round(Path(mem_path).stat().st_size / 1024 / 1024, 1) if Path(mem_path).exists() else 0

        return json.dumps({
            "vm_id": params.vm_id,
            "snapshot_id": snapshot_id,
            "status": "paused",
            "mem_snapshot_mb": mem_size_mb,
            "snapshot_path": str(snap_dir),
            "message": "VM paused. Use vm_resume to restore full state."
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": f"Failed to pause VM: {e}"})


@mcp.tool(
    name="vm_resume",
    annotations={
        "title": "Resume a paused microVM from snapshot",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
)
async def vm_resume(params: VMResumeInput, ctx: Context) -> str:
    """Restore a paused microVM from its memory and disk snapshot.

    Launches a new Firecracker process and loads the saved snapshot, restoring
    the VM to the exact state it was in when paused — including all running
    processes, memory contents, and open files.

    Args:
        params (VMResumeInput):
            - vm_id (str): VM ID to resume
            - snapshot_id (Optional[str]): Specific snapshot to restore; defaults to latest

    Returns:
        str: JSON with vm_id, status, and restoration details
    """
    record = _vm_state.get(params.vm_id)
    if not record:
        return json.dumps({"error": f"VM '{params.vm_id}' not found."})
    if record["status"] != "paused":
        return json.dumps({"error": f"VM must be paused to resume (status: {record['status']})"})

    snapshots = record.get("snapshots", [])
    if not snapshots:
        return json.dumps({"error": "No snapshots found for this VM."})

    # Pick snapshot
    if params.snapshot_id:
        snap = next((s for s in snapshots if s["snapshot_id"] == params.snapshot_id), None)
        if not snap:
            return json.dumps({"error": f"Snapshot '{params.snapshot_id}' not found."})
    else:
        snap = snapshots[-1]  # Latest

    mem_path = snap["mem_path"]
    state_path = snap["state_path"]

    try:
        socket = _socket_path(params.vm_id)
        # Remove stale socket
        if Path(socket).exists():
            Path(socket).unlink()

        await ctx.report_progress(0.2, "Starting Firecracker process...")

        fc_proc = subprocess.Popen(
            [FC_BINARY, "--api-sock", socket, "--level", "Warning"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait for socket
        for _ in range(30):
            if Path(socket).exists():
                break
            await asyncio.sleep(0.2)
        else:
            raise RuntimeError("Firecracker socket never appeared on resume")

        fc = FirecrackerClient(socket)

        await ctx.report_progress(0.5, "Loading snapshot...")
        # Load snapshot
        await fc.put("/snapshot/load", {
            "snapshot_path": state_path,
            "mem_file_path": mem_path,
            "enable_diff_snapshots": False,
            "resume_vm": True  # Auto-resume after load
        })

        await fc.close()
        await ctx.report_progress(0.8, "Waiting for SSH...")

        ssh_port = record["ssh_port"]
        ready = await _wait_for_ssh(ssh_port, timeout=30)

        _vm_state.update(params.vm_id, {
            "status": "running",
            "pid": fc_proc.pid,
        })

        return json.dumps({
            "vm_id": params.vm_id,
            "name": record["name"],
            "status": "running",
            "restored_from_snapshot": snap["snapshot_id"],
            "ssh_port": ssh_port,
            "ssh_ready": ready,
            "message": "VM resumed from snapshot. All prior processes and state are restored."
        }, indent=2)

    except Exception as e:
        _vm_state.update(params.vm_id, {"status": "error", "error": str(e)})
        return json.dumps({"error": f"Failed to resume VM: {e}"})


@mcp.tool(
    name="vm_list",
    annotations={
        "title": "List all microVMs",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def vm_list() -> str:
    """List all microVMs and their current status.

    Returns a summary of all VMs including their IDs, names, status,
    resource allocation, and snapshot count.

    Returns:
        str: JSON list of VM records with status, resource info, and snapshot count
    """
    vms = _vm_state.list_all()
    result = []
    for v in vms:
        result.append({
            "vm_id": v["vm_id"],
            "name": v["name"],
            "status": v["status"],
            "vcpu": v.get("vcpu"),
            "mem_mb": v.get("mem_mb"),
            "ssh_port": v.get("ssh_port"),
            "snapshot_count": len(v.get("snapshots", [])),
            "latest_snapshot": v.get("latest_snapshot"),
            "created_at": v.get("created_at"),
        })
    return json.dumps({"vms": result, "count": len(result)}, indent=2)


@mcp.tool(
    name="vm_status",
    annotations={
        "title": "Get microVM status and snapshot history",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def vm_status(params: VMIDInput) -> str:
    """Get detailed status and snapshot history for a specific VM.

    Args:
        params (VMIDInput): - vm_id (str): VM to inspect

    Returns:
        str: JSON with full VM details including all snapshots
    """
    record = _vm_state.get(params.vm_id)
    if not record:
        return json.dumps({"error": f"VM '{params.vm_id}' not found."})

    snap_details = []
    for s in record.get("snapshots", []):
        mem_path = Path(s["mem_path"])
        size_mb = round(mem_path.stat().st_size / 1024 / 1024, 1) if mem_path.exists() else 0
        snap_details.append({
            "snapshot_id": s["snapshot_id"],
            "created_at": s["created_at"],
            "mem_size_mb": size_mb,
        })

    return json.dumps({
        "vm_id": record["vm_id"],
        "name": record["name"],
        "status": record["status"],
        "vcpu": record.get("vcpu"),
        "mem_mb": record.get("mem_mb"),
        "disk_mb": record.get("disk_mb"),
        "ssh_port": record.get("ssh_port"),
        "pid": record.get("pid"),
        "created_at": record.get("created_at"),
        "snapshots": snap_details,
        "latest_snapshot": record.get("latest_snapshot"),
        "error": record.get("error"),
    }, indent=2)


@mcp.tool(
    name="vm_destroy",
    annotations={
        "title": "Destroy a microVM and delete all its data",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    }
)
async def vm_destroy(params: VMIDInput) -> str:
    """Permanently destroy a microVM, killing its process and deleting all data.

    WARNING: This deletes the VM's disk overlay and all snapshots. This action
    is irreversible. If you want to preserve state, use vm_pause first.

    Args:
        params (VMIDInput): - vm_id (str): VM ID to destroy

    Returns:
        str: JSON confirmation
    """
    record = _vm_state.get(params.vm_id)
    if not record:
        return json.dumps({"error": f"VM '{params.vm_id}' not found."})

    # Kill process
    pid = record.get("pid")
    if pid:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass

    # Remove socket
    socket = Path(_socket_path(params.vm_id))
    if socket.exists():
        socket.unlink()

    # Remove overlay disk
    overlay = _overlay_path(params.vm_id)
    if overlay.exists():
        overlay.unlink()

    # Remove snapshots
    snap_dir = _snapshot_dir(params.vm_id)
    if snap_dir.exists():
        import shutil
        shutil.rmtree(snap_dir)

    _vm_state.delete(params.vm_id)

    return json.dumps({
        "vm_id": params.vm_id,
        "name": record["name"],
        "status": "destroyed",
        "message": "VM and all associated data have been permanently deleted."
    }, indent=2)


# ─── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Firecracker Bash MCP Server")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    mcp.run(transport="streamable-http", host=args.host, port=args.port)
