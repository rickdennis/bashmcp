"""
firecracker_bash_mcp — Firecracker microVM server.

MCP tool:   bash_exec       → /mcp
REST API:   VM lifecycle    → /vms/*
Docs:       Swagger UI      → /docs
"""

import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field, ConfigDict

# ─── Constants ────────────────────────────────────────────────────────────────

BASE_DIR = Path(os.environ.get("FC_BASE_DIR", "./data"))
VM_IMAGES_DIR = BASE_DIR / "vm-images"
SNAPSHOTS_DIR = BASE_DIR / "snapshots"
SOCKETS_DIR = BASE_DIR / "sockets"
STATE_FILE = BASE_DIR / "vm-state.json"

BASE_ROOTFS = VM_IMAGES_DIR / "ubuntu-22.04-base.ext4"
KERNEL_IMAGE = VM_IMAGES_DIR / "vmlinux-5.10"

FC_BINARY = os.environ.get("FC_BINARY", "/usr/bin/firecracker")

DEFAULT_VCPU = 2
DEFAULT_MEM_MB = 512
DEFAULT_DISK_MB = 2048
VM_SSH_START_PORT = 50000

SSH_KEY_PATH = BASE_DIR / "vm_ssh_key"
SESSION_VM_FILE = BASE_DIR / "session-vm.json"

# Per-request context set by MCPRouter
_mcp_session_id: ContextVar[str] = ContextVar("mcp_session_id", default="")
_mcp_client_ip: ContextVar[str] = ContextVar("mcp_client_ip", default="")

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


_vm_state = VMState()


# ─── Session → VM mapping ─────────────────────────────────────────────────────

class SessionVMMap:
    """Persisted mapping: mcp-session-id → vm_id, client-ip → vm_id."""

    def __init__(self):
        self._session: Dict[str, str] = {}  # session_id → vm_id
        self._ip: Dict[str, str] = {}       # client_ip  → vm_id
        self._load()

    def _load(self):
        if SESSION_VM_FILE.exists():
            try:
                data = json.loads(SESSION_VM_FILE.read_text())
                self._session = data.get("session", {})
                self._ip = data.get("ip", {})
            except Exception:
                pass

    def _save(self):
        SESSION_VM_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_VM_FILE.write_text(json.dumps(
            {"session": self._session, "ip": self._ip}, indent=2
        ))

    def get(self, session_id: str, client_ip: str) -> Optional[str]:
        return self._session.get(session_id) or self._ip.get(client_ip)

    def set(self, session_id: str, client_ip: str, vm_id: str):
        if session_id:
            self._session[session_id] = vm_id
        if client_ip:
            self._ip[client_ip] = vm_id
        self._save()

    def remove(self, vm_id: str):
        self._session = {k: v for k, v in self._session.items() if v != vm_id}
        self._ip = {k: v for k, v in self._ip.items() if v != vm_id}
        self._save()


_session_map = SessionVMMap()


# ─── Firecracker API Client ────────────────────────────────────────────────────

class FirecrackerClient:
    def __init__(self, socket_path: str):
        transport = httpx.AsyncHTTPTransport(uds=socket_path)
        self._client = httpx.AsyncClient(
            transport=transport, base_url="http://localhost", timeout=30.0
        )

    async def put(self, path: str, body: Dict) -> Dict:
        r = await self._client.put(path, json=body)
        if r.is_error:
            raise RuntimeError(f"Firecracker PUT {path} → {r.status_code}: {r.text}")
        return r.json() if r.content else {}

    async def patch(self, path: str, body: Dict) -> Dict:
        r = await self._client.patch(path, json=body)
        if r.is_error:
            raise RuntimeError(f"Firecracker PATCH {path} → {r.status_code}: {r.text}")
        return r.json() if r.content else {}

    async def close(self):
        await self._client.aclose()


# ─── VM Lifecycle Helpers ──────────────────────────────────────────────────────

def _overlay_path(vm_id: str) -> Path:
    return BASE_DIR / "overlays" / f"{vm_id}.ext4"

def _socket_path(vm_id: str) -> str:
    return str(SOCKETS_DIR / f"{vm_id}.sock")

def _snapshot_dir(vm_id: str) -> Path:
    return SNAPSHOTS_DIR / vm_id

def _vm_ip(vm_id: str) -> str:
    record = _vm_state.get(vm_id)
    if record and record.get("ip_address"):
        return record["ip_address"]
    return f"172.16.0.{_vm_index(vm_id)}"

def _vm_index(vm_id: str) -> int:
    for i, v in enumerate(_vm_state.list_all()):
        if v["vm_id"] == vm_id:
            return i + 2
    return 2

def _gen_mac(vm_id: str) -> str:
    h = vm_id.replace("-", "")[:10]
    return f"AA:FC:{h[0:2]}:{h[2:4]}:{h[4:6]}:{h[6:8]}"


async def _create_overlay(vm_id: str, size_mb: int):
    overlay = _overlay_path(vm_id)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "cp", "--sparse=always", str(BASE_ROOTFS), str(overlay),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to create overlay: {stderr.decode()}")
    proc2 = await asyncio.create_subprocess_exec(
        "resize2fs", str(overlay), f"{size_mb}M",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await proc2.communicate()


async def _launch_firecracker(vm_id: str, vcpu: int, mem_mb: int) -> int:
    socket = _socket_path(vm_id)
    SOCKETS_DIR.mkdir(parents=True, exist_ok=True)

    fc_proc = subprocess.Popen(
        [FC_BINARY, "--api-sock", socket, "--level", "Warning"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _vm_state.update(vm_id, {"pid": fc_proc.pid})
    for _ in range(30):
        if Path(socket).exists():
            break
        await asyncio.sleep(0.2)
    else:
        fc_proc.kill()
        raise RuntimeError("Firecracker socket never appeared")

    fc = FirecrackerClient(socket)
    try:
        await fc.put("/boot-source", {
            "kernel_image_path": str(KERNEL_IMAGE),
            "boot_args": (
                f"console=ttyS0 reboot=k panic=1 pci=off "
                f"ip=172.16.0.{_vm_index(vm_id)}::172.16.0.1:255.255.255.0::eth0:off "
            )
        })
        await fc.put("/drives/rootfs", {
            "drive_id": "rootfs",
            "path_on_host": str(_overlay_path(vm_id)),
            "is_root_device": True,
            "is_read_only": False
        })
        await fc.put("/machine-config", {"vcpu_count": vcpu, "mem_size_mib": mem_mb, "smt": False})
        await fc.put("/network-interfaces/eth0", {
            "iface_id": "eth0",
            "guest_mac": _gen_mac(vm_id),
            "host_dev_name": f"fc-tap-{(_vm_index(vm_id) - 2):08x}"
        })
        await fc.put("/vsock", {
            "guest_cid": 3,
            "uds_path": str(SOCKETS_DIR / f"{vm_id}-vsock.sock")
        })
        await fc.put("/actions", {"action_type": "InstanceStart"})
    except Exception:
        fc_proc.kill()
        raise
    finally:
        await fc.close()
    return fc_proc.pid


async def _wait_for_ssh(vm_id: str, timeout: int = 30) -> bool:
    ip = _vm_ip(vm_id)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, 22), timeout=2
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            await asyncio.sleep(1)
    return False


async def _ssh_exec(vm_id: str, command: str, timeout: int = 60) -> Dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "-i", str(SSH_KEY_PATH),
        f"root@{_vm_ip(vm_id)}",
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


async def _resolve_session_vm() -> str:
    """Return a running vm_id for the current MCP session, creating or resuming as needed."""
    session_id = _mcp_session_id.get()
    client_ip = _mcp_client_ip.get()

    vm_id = _session_map.get(session_id, client_ip)
    if vm_id:
        record = _vm_state.get(vm_id)
        if record:
            if record["status"] == "running":
                return vm_id
            if record["status"] == "paused":
                log.info(f"Auto-resuming VM {vm_id} for session {session_id or client_ip}")
                snap = record.get("snapshot")
                if not snap:
                    raise RuntimeError(f"VM {vm_id} is paused but has no snapshot.")
                socket = _socket_path(vm_id)
                if Path(socket).exists():
                    Path(socket).unlink()
                vsock = SOCKETS_DIR / f"{vm_id}-vsock.sock"
                if vsock.exists():
                    vsock.unlink()
                fc_proc = subprocess.Popen(
                    [FC_BINARY, "--api-sock", socket, "--level", "Warning"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                _vm_state.update(vm_id, {"pid": fc_proc.pid})
                for _ in range(30):
                    if Path(socket).exists():
                        break
                    await asyncio.sleep(0.2)
                else:
                    fc_proc.kill()
                    raise RuntimeError("Firecracker socket never appeared on auto-resume")
                fc = FirecrackerClient(socket)
                try:
                    await fc.put("/snapshot/load", {
                        "snapshot_path": snap["state_path"], "mem_file_path": snap["mem_path"],
                        "enable_diff_snapshots": False, "resume_vm": True,
                    })
                finally:
                    await fc.close()
                await _wait_for_ssh(vm_id, timeout=30)
                _vm_state.update(vm_id, {"status": "running"})
                return vm_id

    # No usable VM found — create a new one
    log.info(f"Auto-creating VM for session {session_id or client_ip}")
    new_vm_id = str(uuid.uuid4())
    name = f"vm-{new_vm_id[:8]}"
    # Ensure unique name
    existing_names = {v["name"] for v in _vm_state.list_all()}
    base, n = name, 1
    while name in existing_names:
        name = f"{base}-{n}"
        n += 1

    idx = len(_vm_state.list_all()) + 2
    ssh_port = VM_SSH_START_PORT + len(_vm_state.list_all())
    record = {
        "vm_id": new_vm_id, "name": name, "status": "creating",
        "vcpu": DEFAULT_VCPU, "mem_mb": DEFAULT_MEM_MB, "disk_mb": DEFAULT_DISK_MB,
        "ip_address": f"172.16.0.{idx}",
        "ssh_port": ssh_port, "created_at": time.time(), "pid": None, "snapshot": None,
    }
    _vm_state.create(new_vm_id, record)
    try:
        await _create_overlay(new_vm_id, DEFAULT_DISK_MB)
        pid = await _launch_firecracker(new_vm_id, DEFAULT_VCPU, DEFAULT_MEM_MB)
        _vm_state.update(new_vm_id, {"pid": pid, "status": "booting"})
        if not await _wait_for_ssh(new_vm_id, timeout=45):
            raise RuntimeError("SSH never became available after VM boot")
        _vm_state.update(new_vm_id, {"status": "running"})
    except Exception as e:
        _vm_state.update(new_vm_id, {"status": "error", "error": str(e)})
        raise

    _session_map.set(session_id, client_ip, new_vm_id)
    return new_vm_id


# ─── Pydantic Models ───────────────────────────────────────────────────────────

class VMCreateInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: Optional[str] = Field(default=None, description="Human-readable label (auto-generated if omitted)", max_length=64)
    vcpu: int = Field(default=DEFAULT_VCPU, description="Number of vCPUs", ge=1, le=8)
    mem_mb: int = Field(default=DEFAULT_MEM_MB, description="RAM in MB", ge=128, le=8192)
    disk_mb: int = Field(default=DEFAULT_DISK_MB, description="Disk size in MB", ge=512, le=20480)




class BashExecInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    vm_id: Optional[str] = Field(default=None, description="VM ID. If omitted, auto-creates or resumes the VM for this MCP session.")
    command: str = Field(..., description="Shell command to execute (runs as root)", min_length=1, max_length=8192)
    timeout: int = Field(default=60, description="Max seconds to wait", ge=1, le=600)
    working_dir: Optional[str] = Field(default=None, description="Working directory inside the VM")


# ─── FastMCP (bash_exec only) ──────────────────────────────────────────────────

mcp = FastMCP(
    "firecracker_bash_mcp",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


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
    """Run a shell command as root inside a running Firecracker microVM via SSH.

    Args:
        params (BashExecInput):
            - vm_id (Optional[str]): Target VM ID. If omitted, auto-creates or resumes the session VM.
            - command (str): Bash command to run
            - timeout (int): Max seconds to wait (default: 60, max: 600)
            - working_dir (Optional[str]): Working directory inside VM

    Returns:
        str: JSON with stdout, stderr, returncode, and elapsed_seconds
    """
    try:
        vm_id = params.vm_id or await _resolve_session_vm()
    except Exception as e:
        return json.dumps({"error": f"Could not resolve VM for session: {e}"})

    record = _vm_state.get(vm_id)
    if not record:
        return json.dumps({"error": f"VM '{vm_id}' not found."})
    if record["status"] != "running":
        return json.dumps({
            "error": f"VM is not running (status: {record['status']}). "
                     f"Use POST /vms/{{vm_id}}/resume if paused."
        })

    command = params.command
    if params.working_dir:
        command = f"cd {params.working_dir} && {command}"

    start = time.time()
    result = await _ssh_exec(vm_id, command, timeout=params.timeout)
    elapsed = round(time.time() - start, 2)

    return json.dumps({
        "vm_id": vm_id,
        "command": params.command,
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        "returncode": result["returncode"],
        "elapsed_seconds": elapsed,
    }, indent=2)


# ─── FastAPI REST API + Docs ───────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    for d in [BASE_DIR, VM_IMAGES_DIR, SNAPSHOTS_DIR, SOCKETS_DIR, BASE_DIR / "overlays"]:
        d.mkdir(parents=True, exist_ok=True)
    log.info(f"Firecracker server started. Base dir: {BASE_DIR}")
    # Run MCP session manager alongside FastAPI
    async with mcp._session_manager.run():
        yield


# Build MCP ASGI handler (also initializes _session_manager)
_mcp_starlette = mcp.streamable_http_app()
_mcp_handler = _mcp_starlette.routes[0].endpoint

api = FastAPI(
    title="Firecracker VM API",
    version="1.0.0",
    description="REST API for managing Firecracker microVMs. MCP tool `bash_exec` available at `/mcp`.",
    lifespan=lifespan,
)


# ─── ASGI middleware: route /mcp to FastMCP, everything else to FastAPI ────────

class MCPRouter:
    def __init__(self, api_app, mcp_handler):
        self.api_app = api_app
        self.mcp_handler = mcp_handler

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").rstrip("/") == "/mcp":
            scope = dict(scope)
            headers = dict(scope.get("headers", []))

            session_id = headers.get(b"mcp-session-id", b"").decode()
            client_ip = scope.get("client", ("", 0))[0]
            _mcp_session_id.set(session_id)
            _mcp_client_ip.set(client_ip)

            scope["headers"] = [
                (k, v) for k, v in scope.get("headers", []) if k.lower() != b"host"
            ] + [(b"host", b"localhost")]
            await self.mcp_handler(scope, receive, send)
        else:
            await self.api_app(scope, receive, send)


# ─── REST Endpoints ────────────────────────────────────────────────────────────

@api.post("/vms", status_code=201, tags=["VMs"], summary="Create a new microVM")
async def api_vm_create(params: VMCreateInput):
    """Create and boot a new Firecracker microVM. Returns a `vm_id` to use with `bash_exec`."""
    vm_id = str(uuid.uuid4())
    name = params.name or f"vm-{vm_id[:8]}"
    if any(v.get("name") == name for v in _vm_state.list_all()):
        raise HTTPException(status_code=409, detail=f"VM named '{name}' already exists.")

    idx = len(_vm_state.list_all()) + 2
    ssh_port = VM_SSH_START_PORT + len(_vm_state.list_all())
    record = {
        "vm_id": vm_id, "name": name, "status": "creating",
        "vcpu": params.vcpu, "mem_mb": params.mem_mb, "disk_mb": params.disk_mb,
        "ip_address": f"172.16.0.{idx}",
        "ssh_port": ssh_port, "created_at": time.time(), "pid": None, "snapshot": None,
    }
    _vm_state.create(vm_id, record)

    try:
        await _create_overlay(vm_id, params.disk_mb)
        pid = await _launch_firecracker(vm_id, params.vcpu, params.mem_mb)
        _vm_state.update(vm_id, {"pid": pid, "status": "booting"})
        ready = await _wait_for_ssh(vm_id, timeout=45)
        if not ready:
            _vm_state.update(vm_id, {"status": "error", "error": "SSH timeout"})
            raise HTTPException(status_code=500, detail="VM booted but SSH never became available.")
        _vm_state.update(vm_id, {"status": "running"})
        return {
            "vm_id": vm_id, "name": name, "status": "running",
            "ssh_port": ssh_port, "ip_address": f"172.16.0.{_vm_index(vm_id)}",
            "vcpu": params.vcpu, "mem_mb": params.mem_mb, "disk_mb": params.disk_mb,
        }
    except HTTPException:
        raise
    except Exception as e:
        _vm_state.update(vm_id, {"status": "error", "error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))


@api.get("/vms", tags=["VMs"], summary="List all microVMs")
async def api_vm_list():
    vms = _vm_state.list_all()
    return {
        "vms": [
            {
                "vm_id": v["vm_id"], "name": v["name"], "status": v["status"],
                "vcpu": v.get("vcpu"), "mem_mb": v.get("mem_mb"),
                "ssh_port": v.get("ssh_port"),
                "has_snapshot": v.get("snapshot") is not None,
                "created_at": v.get("created_at"),
            }
            for v in vms
        ],
        "count": len(vms),
    }


@api.get("/vms/{vm_id}", tags=["VMs"], summary="Get VM status")
async def api_vm_status(vm_id: str):
    record = _vm_state.get(vm_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"VM '{vm_id}' not found.")

    snap = record.get("snapshot")
    snap_info = None
    if snap:
        mem_path = Path(snap["mem_path"])
        snap_info = {
            "created_at": snap["created_at"],
            "mem_size_mb": round(mem_path.stat().st_size / 1024 / 1024, 1) if mem_path.exists() else 0,
        }

    return {
        "vm_id": record["vm_id"], "name": record["name"], "status": record["status"],
        "vcpu": record.get("vcpu"), "mem_mb": record.get("mem_mb"),
        "disk_mb": record.get("disk_mb"), "ssh_port": record.get("ssh_port"),
        "pid": record.get("pid"), "created_at": record.get("created_at"),
        "snapshot": snap_info,
        "error": record.get("error"),
    }


@api.post("/vms/{vm_id}/pause", tags=["VMs"], summary="Pause VM and save snapshot")
async def api_vm_pause(vm_id: str):
    record = _vm_state.get(vm_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"VM '{vm_id}' not found.")
    if record["status"] != "running":
        raise HTTPException(status_code=409, detail=f"VM must be running to pause (status: {record['status']}).")

    snap_dir = _snapshot_dir(vm_id)
    snap_dir.mkdir(parents=True, exist_ok=True)
    mem_path = str(snap_dir / "memory.bin")
    state_path = str(snap_dir / "vmstate.bin")

    try:
        fc = FirecrackerClient(_socket_path(vm_id))
        await fc.patch("/vm", {"state": "Paused"})
        await fc.put("/snapshot/create", {
            "snapshot_type": "Full", "snapshot_path": state_path,
            "mem_file_path": mem_path,
        })
        await fc.close()

        pid = record.get("pid")
        if pid:
            try:
                os.kill(pid, 15)
                await asyncio.sleep(1)
                os.kill(pid, 9)
            except ProcessLookupError:
                pass

        snapshot_entry = {
            "created_at": time.time(),
            "mem_path": mem_path, "state_path": state_path,
        }
        _vm_state.update(vm_id, {
            "status": "paused", "pid": None,
            "snapshot": snapshot_entry,
        })

        mem_size_mb = round(Path(mem_path).stat().st_size / 1024 / 1024, 1) if Path(mem_path).exists() else 0
        return {
            "vm_id": vm_id, "status": "paused",
            "mem_snapshot_mb": mem_size_mb,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to pause VM: {e}")


@api.post("/vms/{vm_id}/resume", tags=["VMs"], summary="Resume VM from snapshot")
async def api_vm_resume(vm_id: str):
    record = _vm_state.get(vm_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"VM '{vm_id}' not found.")
    if record["status"] != "paused":
        raise HTTPException(status_code=409, detail=f"VM must be paused to resume (status: {record['status']}).")

    snap = record.get("snapshot")
    if not snap:
        raise HTTPException(status_code=409, detail="No snapshot found for this VM.")

    try:
        socket = _socket_path(vm_id)
        if Path(socket).exists():
            Path(socket).unlink()
        vsock = SOCKETS_DIR / f"{vm_id}-vsock.sock"
        if vsock.exists():
            vsock.unlink()

        fc_proc = subprocess.Popen(
            [FC_BINARY, "--api-sock", socket, "--level", "Warning"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(30):
            if Path(socket).exists():
                break
            await asyncio.sleep(0.2)
        else:
            raise RuntimeError("Firecracker socket never appeared on resume")

        fc = FirecrackerClient(socket)
        await fc.put("/snapshot/load", {
            "snapshot_path": snap["state_path"], "mem_file_path": snap["mem_path"],
            "enable_diff_snapshots": False, "resume_vm": True
        })
        await fc.close()

        ready = await _wait_for_ssh(vm_id, timeout=30)
        _vm_state.update(vm_id, {"status": "running", "pid": fc_proc.pid})

        return {
            "vm_id": vm_id, "name": record["name"], "status": "running", "ssh_ready": ready,
        }
    except Exception as e:
        _vm_state.update(vm_id, {"status": "error", "error": str(e)})
        raise HTTPException(status_code=500, detail=f"Failed to resume VM: {e}")


@api.delete("/vms/{vm_id}", tags=["VMs"], summary="Destroy a VM and delete all data")
async def api_vm_destroy(vm_id: str):
    record = _vm_state.get(vm_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"VM '{vm_id}' not found.")

    pid = record.get("pid")
    if pid:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass

    socket = Path(_socket_path(vm_id))
    if socket.exists():
        socket.unlink()

    overlay = _overlay_path(vm_id)
    if overlay.exists():
        overlay.unlink()

    snap_dir = _snapshot_dir(vm_id)
    if snap_dir.exists():
        shutil.rmtree(snap_dir)

    _vm_state.delete(vm_id)
    _session_map.remove(vm_id)
    return {"vm_id": vm_id, "name": record["name"], "status": "destroyed"}


# ─── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Firecracker VM server")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    uvicorn.run(MCPRouter(api, _mcp_handler), host=args.host, port=args.port)
