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
import base64
import secrets
import shlex
import tempfile
import time
import uuid
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional, Union
from urllib.parse import urlparse

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
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

SSH_KEY_PATH = BASE_DIR / "vm_ssh_key"
SESSION_VM_FILE = BASE_DIR / "session-vm.json"
SLOTS_FILE = BASE_DIR / "slots.json"

# A VM's slot index determines its bridge IP (172.16.0.{slot}) and tap device
# (fc-tap-{slot-2:08x}). setup-network.sh pre-creates 32 taps, so slots run
# SLOT_MIN..SLOT_MAX inclusive (32 values); the tap count is the hard cap.
SLOT_MIN = 2
SLOT_MAX = 33

# Node identity (Kubernetes downward API spec.nodeName); empty when run standalone.
NODE_NAME = os.environ.get("NODE_NAME", "")

# Auto-pause a VM after this many idle seconds (snapshot + kill FC, freeing RAM and
# the vCPU — resumed VMs busy-loop a core under Firecracker, so idle-pausing keeps
# idle VMs cheap). The next bash_exec auto-resumes it. 0 disables.
IDLE_PAUSE_SECONDS = int(os.environ.get("FC_IDLE_PAUSE_SECONDS", "300"))
IDLE_CHECK_INTERVAL = int(os.environ.get("FC_IDLE_CHECK_INTERVAL", "30"))

# Guest agent (fc-agent) control-plane HTTP port — replaces SSH for command exec.
AGENT_HTTP_PORT = int(os.environ.get("FC_AGENT_HTTP_PORT", "2025"))

# Host source for the in-VM agent runner's Anthropic creds, injected into the overlay at
# /etc/fc-agent-runner/anthropic.env before boot. Precedence: ANTHROPIC_API_KEY env, else
# this file's contents. Missing/empty => not injected (bash-only VMs are unaffected).
RUNNER_ENV_FILE = os.environ.get("FC_RUNNER_ENV_FILE", "/etc/fc-agent-runner/anthropic.env")

# Opt-in: freeze the guest root fs around snapshot/create for a filesystem-consistent
# overlay (chiefly for S3-archived disks). Off by default — for plain pause/resume the
# atomic memory+disk snapshot is already consistent. See _pause_vm / _agent_fs_freeze.
FS_FREEZE_ON_SNAPSHOT = os.environ.get("FC_FS_FREEZE_ON_SNAPSHOT", "").lower() in ("1", "true", "yes")

# Optional S3 archival of paused-VM snapshots, so a paused VM survives node loss and
# can be restored on a survivor node. Disabled when FC_S3_BUCKET is unset. Credentials
# come from the boto3 default chain (EC2 instance role via IMDS on the host; IRSA/Secret
# on EKS). Objects: s3://<bucket>/<prefix>/<vm_id>/{overlay.ext4,memory.bin,vmstate.bin,meta.json}
FC_S3_BUCKET = os.environ.get("FC_S3_BUCKET", "")
FC_S3_PREFIX = os.environ.get("FC_S3_PREFIX", "fc-mcp/snapshots").strip("/")
S3_ENABLED = bool(FC_S3_BUCKET)


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
    """Persisted mapping: mcp-session-id → vm_id."""

    def __init__(self):
        self._session: Dict[str, str] = {}
        self._load()

    def _load(self):
        if SESSION_VM_FILE.exists():
            try:
                data = json.loads(SESSION_VM_FILE.read_text())
                self._session = data.get("session", {})
            except Exception:
                pass

    def _save(self):
        SESSION_VM_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_VM_FILE.write_text(json.dumps({"session": self._session}, indent=2))

    def get(self, session_id: str) -> Optional[str]:
        return self._session.get(session_id)

    def set(self, session_id: str, vm_id: str):
        if session_id:
            self._session[session_id] = vm_id
            self._save()

    def remove(self, vm_id: str):
        self._session = {k: v for k, v in self._session.items() if v != vm_id}
        self._save()


_session_map = SessionVMMap()


# ─── Slot Allocator (IP / tap index) ───────────────────────────────────────────

class SlotAllocator:
    """Persistent node-local free-list of VM slot indices (SLOT_MIN..SLOT_MAX).

    A slot is allocated once and stored on the VM record, so destroying one VM
    never shifts another's IP/tap. This replaces the old positional indexing
    (``len(list_all()) + 2``), which drifted a survivor's tap/boot-IP away from
    its frozen stored IP whenever an earlier VM was destroyed.
    """

    def __init__(self):
        self._used: set = set()
        self._free: List[int] = list(range(SLOT_MIN, SLOT_MAX + 1))
        self._load()

    def _load(self):
        if SLOTS_FILE.exists():
            try:
                data = json.loads(SLOTS_FILE.read_text())
                self.rebuild_from_records(data.get("used", []))
            except Exception as e:
                log.warning(f"Could not load slots file: {e}")

    def _save(self):
        SLOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SLOTS_FILE.write_text(json.dumps({"used": sorted(self._used)}, indent=2))

    def rebuild_from_records(self, in_use: List[int]):
        """Authoritatively reset the free-list from a list of in-use slots."""
        self._used = {s for s in in_use if s is not None and SLOT_MIN <= s <= SLOT_MAX}
        self._free = [i for i in range(SLOT_MIN, SLOT_MAX + 1) if i not in self._used]
        self._save()

    def allocate(self) -> Optional[int]:
        if not self._free:
            return None
        slot = self._free.pop(0)
        self._used.add(slot)
        self._save()
        return slot

    def reserve(self, slot: Optional[int]):
        if slot is None or not (SLOT_MIN <= slot <= SLOT_MAX):
            return
        if slot in self._free:
            self._free.remove(slot)
        self._used.add(slot)
        self._save()

    def release(self, slot: Optional[int]):
        if slot is None or not (SLOT_MIN <= slot <= SLOT_MAX):
            return
        self._used.discard(slot)
        if slot not in self._free:
            self._free.append(slot)
            self._free.sort()
        self._save()

    def free_count(self) -> int:
        return len(self._free)


_slots = SlotAllocator()

# Guards slot allocation + state-record creation so interleaved awaits in a single
# process cannot double-issue a slot or corrupt the registry (no locking existed before).
_create_lock = asyncio.Lock()

# Flipped True by reconcile_on_startup(); gates the /ready probe.
_ready = False


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

def _record_slot(record: Optional[Dict]) -> Optional[int]:
    """The VM's stored slot, or derived from its stored IP for legacy records."""
    if not record:
        return None
    if record.get("slot") is not None:
        return record["slot"]
    ip = record.get("ip_address")
    if ip:
        try:
            return int(ip.rsplit(".", 1)[1])
        except (ValueError, IndexError):
            return None
    return None

def _vm_ip(vm_id: str) -> str:
    record = _vm_state.get(vm_id)
    if record and record.get("ip_address"):
        return record["ip_address"]
    slot = _record_slot(record)
    return f"172.16.0.{slot if slot is not None else SLOT_MIN}"

def _vm_tap(vm_id: str) -> str:
    slot = _record_slot(_vm_state.get(vm_id))
    if slot is None:
        slot = SLOT_MIN
    return f"fc-tap-{(slot - SLOT_MIN):08x}"

def _touch(vm_id: str):
    """Mark a VM as active so the idle-pause loop won't reap it mid-use."""
    if _vm_state.get(vm_id):
        _vm_state.update(vm_id, {"last_activity": time.time()})

def _gen_mac(vm_id: str) -> str:
    h = vm_id.replace("-", "")[:10]
    return f"AA:FC:{h[0:2]}:{h[2:4]}:{h[4:6]}:{h[6:8]}"


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours — still alive

def _pid_is_firecracker(pid: Optional[int]) -> bool:
    """Alive AND actually a firecracker process (guards against PID reuse).

    On Linux uses /proc/<pid>/comm; off-Linux (dev) falls back to liveness only.
    """
    if not _pid_alive(pid):
        return False
    comm = Path(f"/proc/{pid}/comm")
    if comm.exists():
        try:
            return comm.read_text().strip() == "firecracker"
        except Exception:
            return False
    return True


# ─── S3 snapshot archival (optional; FC_S3_BUCKET) ──────────────────────────────

def _s3_key(vm_id: str, name: str) -> str:
    return f"{FC_S3_PREFIX}/{vm_id}/{name}"

def _archive_blocking(vm_id: str, record: Dict):
    """Upload overlay + memory + vmstate + meta.json to S3. Blocking — call via to_thread."""
    import boto3
    s3 = boto3.client("s3")
    snap = record.get("snapshot") or {}
    overlay = _overlay_path(vm_id)
    mem = Path(snap.get("mem_path", ""))
    state = Path(snap.get("state_path", ""))
    if overlay.exists():
        s3.upload_file(str(overlay), FC_S3_BUCKET, _s3_key(vm_id, "overlay.ext4"))
    if mem.exists():
        s3.upload_file(str(mem), FC_S3_BUCKET, _s3_key(vm_id, "memory.bin"))
    if state.exists():
        s3.upload_file(str(state), FC_S3_BUCKET, _s3_key(vm_id, "vmstate.bin"))
    meta = {
        "vm_id": vm_id, "name": record.get("name"), "slot": _record_slot(record),
        "ip_address": record.get("ip_address"), "vcpu": record.get("vcpu"),
        "mem_mb": record.get("mem_mb"), "disk_mb": record.get("disk_mb"),
        "created_at": record.get("created_at"),
    }
    s3.put_object(Bucket=FC_S3_BUCKET, Key=_s3_key(vm_id, "meta.json"),
                  Body=json.dumps(meta).encode())

def _restore_blocking(vm_id: str) -> Optional[Dict]:
    """Download meta.json + artifacts from S3 to local paths. Blocking. Returns meta or None."""
    import boto3
    from botocore.exceptions import ClientError
    s3 = boto3.client("s3")
    try:
        obj = s3.get_object(Bucket=FC_S3_BUCKET, Key=_s3_key(vm_id, "meta.json"))
        meta = json.loads(obj["Body"].read())
    except ClientError:
        return None
    overlay = _overlay_path(vm_id); overlay.parent.mkdir(parents=True, exist_ok=True)
    snap_dir = _snapshot_dir(vm_id); snap_dir.mkdir(parents=True, exist_ok=True)
    s3.download_file(FC_S3_BUCKET, _s3_key(vm_id, "overlay.ext4"), str(overlay))
    s3.download_file(FC_S3_BUCKET, _s3_key(vm_id, "memory.bin"), str(snap_dir / "memory.bin"))
    s3.download_file(FC_S3_BUCKET, _s3_key(vm_id, "vmstate.bin"), str(snap_dir / "vmstate.bin"))
    return meta

async def _s3_archive_vm(vm_id: str):
    """Best-effort: archive a paused VM's artifacts to S3 (no-op when disabled)."""
    if not S3_ENABLED:
        return
    record = _vm_state.get(vm_id)
    if not record:
        return
    try:
        await asyncio.to_thread(_archive_blocking, vm_id, record)
        _vm_state.update(vm_id, {"archived_at": time.time()})
        log.info(f"s3-archive: {vm_id} -> s3://{FC_S3_BUCKET}/{_s3_key(vm_id, '')}")
    except Exception as e:
        log.warning(f"s3-archive failed for {vm_id}: {e}")

async def _s3_restore_vm(vm_id: str) -> bool:
    """Download a VM's artifacts from S3 to local paths, rebuild its record, reserve its slot.

    Returns True if restored (artifacts now local + record present as 'paused'). The same slot
    must be free on this node so the snapshot's tap/IP/MAC match.
    """
    if not S3_ENABLED:
        return False
    try:
        meta = await asyncio.to_thread(_restore_blocking, vm_id)
    except Exception as e:
        log.warning(f"s3-restore failed for {vm_id}: {e}")
        return False
    if not meta:
        return False
    snap_dir = _snapshot_dir(vm_id)
    slot = meta.get("slot")
    record = dict(_vm_state.get(vm_id) or {})
    record.update({
        "vm_id": vm_id, "name": meta.get("name") or f"vm-{vm_id[:8]}", "status": "paused",
        "vcpu": meta.get("vcpu", DEFAULT_VCPU), "mem_mb": meta.get("mem_mb", DEFAULT_MEM_MB),
        "disk_mb": meta.get("disk_mb", DEFAULT_DISK_MB), "slot": slot,
        "ip_address": meta.get("ip_address") or (f"172.16.0.{slot}" if slot is not None else None),
        "snapshot": {"created_at": meta.get("created_at"),
                     "mem_path": str(snap_dir / "memory.bin"),
                     "state_path": str(snap_dir / "vmstate.bin")},
        "pid": None, "last_activity": time.time(),
    })
    if slot is not None:
        _slots.reserve(slot)
    _vm_state.create(vm_id, record)
    log.info(f"s3-restore: {vm_id} pulled from S3 (slot {slot})")
    return True

def _delete_blocking(vm_id: str):
    import boto3
    s3 = boto3.client("s3")
    for name in ("overlay.ext4", "memory.bin", "vmstate.bin", "meta.json"):
        s3.delete_object(Bucket=FC_S3_BUCKET, Key=_s3_key(vm_id, name))

async def _s3_delete_vm(vm_id: str):
    """Best-effort: remove a VM's S3 archive (called on destroy so it isn't orphaned)."""
    if not S3_ENABLED:
        return
    try:
        await asyncio.to_thread(_delete_blocking, vm_id)
        log.info(f"s3-delete: removed archive for {vm_id}")
    except Exception as e:
        log.warning(f"s3-delete failed for {vm_id}: {e}")


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
    await _write_agent_token(vm_id)
    await _write_runner_creds(vm_id)
    await _write_egress_ca(vm_id)


async def _write_egress_ca(vm_id: str):
    """Install the egress CA's PUBLIC cert into the VM's trust store so the guest trusts the
    MITM proxy's per-SNI leaves. Mirrors _write_agent_token: loop-mount the overlay pre-boot and
    drop the cert under the system anchors dir (a boot-time `update-ca-certificates` oneshot in
    the rootfs registers it). The CA private key never enters a VM. No-op when egress is disabled
    or the cert is absent."""
    if not FC_EGRESS_ENABLED:
        return
    ca_crt = FC_EGRESS_CA_DIR / "ca.crt"
    if not ca_crt.exists():
        log.warning("FC_EGRESS_ENABLED but %s missing; skipping egress CA install for %s", ca_crt, vm_id)
        return
    overlay = _overlay_path(vm_id)
    mnt = Path(tempfile.mkdtemp(prefix="fc-egca-"))
    try:
        m = await asyncio.create_subprocess_exec(
            "mount", "-o", "loop", str(overlay), str(mnt),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, merr = await m.communicate()
        if m.returncode != 0:
            raise RuntimeError(f"egress-ca mount failed: {merr.decode(errors='replace')}")
        try:
            ca_bytes = ca_crt.read_bytes()
            # 1. Drop into the standard custom-anchor dir for when update-ca-certificates runs.
            anchors = mnt / "usr" / "local" / "share" / "ca-certificates"
            anchors.mkdir(parents=True, exist_ok=True)
            (anchors / "fc-egress.crt").write_bytes(ca_bytes)
            (anchors / "fc-egress.crt").chmod(0o644)
            # 2. Also append directly to the system bundle file that git/libcurl/openssl read
            # without `update-ca-certificates` needing to run first (handles base rootfs that
            # predate the fc-egress-ca.service oneshot).
            bundle = mnt / "etc" / "ssl" / "certs" / "ca-certificates.crt"
            if bundle.exists():
                existing = bundle.read_bytes()
                if ca_bytes not in existing:  # idempotent
                    bundle.write_bytes(existing + b"\n" + ca_bytes)
            # 3. Individual PEM in the certs dir for c_rehash / SSL_CERT_DIR consumers.
            certs_dir = mnt / "etc" / "ssl" / "certs"
            certs_dir.mkdir(parents=True, exist_ok=True)
            (certs_dir / "fc-egress.pem").write_bytes(ca_bytes)
            (certs_dir / "fc-egress.pem").chmod(0o644)
        finally:
            u = await asyncio.create_subprocess_exec(
                "umount", str(mnt),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await u.communicate()
    finally:
        try:
            mnt.rmdir()
        except OSError:
            pass


async def _write_agent_token(vm_id: str):
    """Write the VM's per-VM agent bearer token into the overlay at /etc/fc-agent/token
    (0600) before boot, so fc-agent can authenticate the host. Loop-mounts the overlay
    (host runs as root); the overlay isn't opened by Firecracker until after this."""
    token = (_vm_state.get(vm_id) or {}).get("agent_token")
    if not token:
        return
    overlay = _overlay_path(vm_id)
    mnt = Path(tempfile.mkdtemp(prefix="fc-tok-"))
    try:
        m = await asyncio.create_subprocess_exec(
            "mount", "-o", "loop", str(overlay), str(mnt),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, merr = await m.communicate()
        if m.returncode != 0:
            raise RuntimeError(f"token mount failed: {merr.decode(errors='replace')}")
        try:
            tok_dir = mnt / "etc" / "fc-agent"
            tok_dir.mkdir(parents=True, exist_ok=True)
            tok_path = tok_dir / "token"
            tok_path.write_text(token)
            tok_path.chmod(0o600)
        finally:
            u = await asyncio.create_subprocess_exec(
                "umount", str(mnt),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await u.communicate()
    finally:
        try:
            mnt.rmdir()
        except OSError:
            pass


async def _write_runner_creds(vm_id: str):
    """Inject the in-VM agent runner's Anthropic credentials into the overlay at
    /etc/fc-agent-runner/anthropic.env (0600) before boot. Best-effort and gated: skipped
    when no key source is configured, so bash-only VMs are unaffected. Source: the
    ANTHROPIC_API_KEY env var, else the host file RUNNER_ENV_FILE. (P2 will scope this to
    agent sessions only; today it injects whenever a key source exists.)"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        content = f"ANTHROPIC_API_KEY={key}\n"
    else:
        p = Path(RUNNER_ENV_FILE)
        if not p.exists():
            return
        try:
            content = p.read_text()
        except Exception as e:
            log.warning(f"runner creds read failed ({RUNNER_ENV_FILE}): {e}")
            return
    if not content.strip():
        return
    # Normalize to a safe KEY=VALUE env file: a bare key (no '=' on the first non-empty
    # line) becomes ANTHROPIC_API_KEY=<key>, so the guest never has a stray value to source.
    first = next((ln for ln in content.splitlines() if ln.strip()), "")
    if "=" not in first:
        content = f"ANTHROPIC_API_KEY={content.strip()}\n"
    overlay = _overlay_path(vm_id)
    mnt = Path(tempfile.mkdtemp(prefix="fc-runner-"))
    try:
        m = await asyncio.create_subprocess_exec(
            "mount", "-o", "loop", str(overlay), str(mnt),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, merr = await m.communicate()
        if m.returncode != 0:
            log.warning(f"runner-creds mount failed for {vm_id}: {merr.decode(errors='replace')}")
            return
        try:
            d = mnt / "etc" / "fc-agent-runner"
            d.mkdir(parents=True, exist_ok=True)
            envp = d / "anthropic.env"
            envp.write_text(content)
            envp.chmod(0o600)
        finally:
            u = await asyncio.create_subprocess_exec(
                "umount", str(mnt),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await u.communicate()
    finally:
        try:
            mnt.rmdir()
        except OSError:
            pass


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
                f"ip={_vm_ip(vm_id)}::172.16.0.1:255.255.255.0::eth0:off "
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
            "host_dev_name": _vm_tap(vm_id)
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


# SSH command/readiness path removed — all command execution and readiness go through
# the guest agent (_wait_for_agent / _agent_exec below). The guest no longer runs sshd.


async def _wait_for_agent(vm_id: str, timeout: int = 30) -> bool:
    """Poll the guest agent's /health until it answers 200 or the deadline passes.
    Replaces _wait_for_ssh — /health is up only once the agent's exec loop is live."""
    url = f"http://{_vm_ip(vm_id)}:{AGENT_HTTP_PORT}/health"
    deadline = time.time() + timeout
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.time() < deadline:
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(1)
    return False


async def _agent_exec(vm_id: str, command: str, timeout: int = 60,
                      working_dir: Optional[str] = None) -> Dict[str, Any]:
    """Run a command in the VM via the guest agent, replacing SSH. Uses the async
    (tmux-backed) path: start it (POST /exec_async -> exec_id) then poll /exec_async/{id}
    from our last byte offsets until the .done sentinel appears, retrying transient
    failures. Because the command's lifetime lives in tmux + on-disk files keyed by
    exec_id, it survives a host reconnect AND a VM pause/resume (a paused VM simply
    resumes answering the same exec_id). Returns the same {stdout, stderr, returncode}
    contract as _ssh_exec; working_dir is applied in-guest."""
    record = _vm_state.get(vm_id) or {}
    token = record.get("agent_token", "")
    base = f"http://{_vm_ip(vm_id)}:{AGENT_HTTP_PORT}"
    headers = {"Authorization": f"Bearer {token}"}
    payload: Dict[str, Any] = {"command": command, "timeout_secs": timeout}
    if working_dir:
        payload["working_dir"] = working_dir

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                r = await client.post(f"{base}/exec_async", json=payload, headers=headers)
            except Exception as e:
                return {"stdout": "", "stderr": f"agent connection failed: {e}", "returncode": -1}
            if r.status_code != 200:
                return {"stdout": "", "stderr": f"agent error {r.status_code}: {r.text}", "returncode": -1}
            exec_id = r.json().get("exec_id")
            if not exec_id:
                return {"stdout": "", "stderr": "agent did not return an exec_id", "returncode": -1}

            out_parts: List[str] = []
            err_parts: List[str] = []
            out_off = err_off = 0
            rc = -1
            done = False
            # The in-guest `timeout` enforces the real limit; we allow extra grace for
            # boot/poll/pause hiccups before giving up on the channel itself.
            deadline = time.time() + timeout + 30
            while time.time() < deadline:
                try:
                    p = await client.get(f"{base}/exec_async/{exec_id}",
                                         params={"out": out_off, "err": err_off}, headers=headers)
                    if p.status_code != 200:
                        await asyncio.sleep(0.5)
                        continue
                    d = p.json()
                except Exception:
                    await asyncio.sleep(0.5)  # blip or paused VM — keep re-polling the same exec_id
                    continue
                if d.get("stdout"):
                    out_parts.append(d["stdout"])
                if d.get("stderr"):
                    err_parts.append(d["stderr"])
                out_off = d.get("out_offset", out_off)
                err_off = d.get("err_offset", err_off)
                if d.get("done"):
                    rc = d.get("returncode", -1)
                    done = True
                    break
                await asyncio.sleep(0.2)

            try:
                await client.delete(f"{base}/exec_async/{exec_id}", headers=headers)
            except Exception:
                pass

            stdout = "".join(out_parts)
            stderr = "".join(err_parts)
            if not done or rc == 124:  # 124 = in-guest `timeout` killed it
                if stderr and not stderr.endswith("\n"):
                    stderr += "\n"
                reason = "agent exec timed out" if not done else "command timed out"
                return {"stdout": stdout, "stderr": stderr + reason, "returncode": -1}
            return {"stdout": stdout, "stderr": stderr, "returncode": rc}
    except Exception as e:
        return {"stdout": "", "stderr": f"agent exec failed: {e}", "returncode": -1}


async def _agent_fs_freeze(vm_id: str) -> bool:
    """Best-effort: freeze the guest root fs so the snapshot's overlay is filesystem-
    consistent. The agent arms an auto-thaw watchdog in case we never thaw (host crash)."""
    record = _vm_state.get(vm_id) or {}
    token = record.get("agent_token", "")
    url = f"http://{_vm_ip(vm_id)}:{AGENT_HTTP_PORT}/fs_freeze"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, headers={"Authorization": f"Bearer {token}"})
        return r.status_code == 200
    except Exception as e:
        log.warning(f"fs_freeze failed for {vm_id}: {e}")
        return False


async def _agent_fs_thaw(vm_id: str) -> bool:
    """Best-effort, idempotent: thaw the guest root fs after resuming a VM whose snapshot
    was taken frozen (the restored guest kernel still has the superblock frozen)."""
    record = _vm_state.get(vm_id) or {}
    token = record.get("agent_token", "")
    url = f"http://{_vm_ip(vm_id)}:{AGENT_HTTP_PORT}/fs_thaw"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, headers={"Authorization": f"Bearer {token}"})
        return r.status_code == 200
    except Exception as e:
        log.warning(f"fs_thaw failed for {vm_id}: {e}")
        return False


async def _allocate_and_create_record(vm_id: str, name: str, vcpu: int, mem_mb: int, disk_mb: int) -> Dict:
    """Atomically allocate a slot and persist a 'creating' record.

    Raises RuntimeError("node at capacity") when every slot is in use, or
    ValueError on a duplicate name. Held under _create_lock so interleaved
    awaits in this single process cannot double-issue a slot.
    """
    async with _create_lock:
        if any(v.get("name") == name for v in _vm_state.list_all()):
            raise ValueError(f"VM named '{name}' already exists.")
        slot = _slots.allocate()
        if slot is None:
            raise RuntimeError(f"node at capacity: all {SLOT_MAX - SLOT_MIN + 1} VM slots in use")
        record = {
            "vm_id": vm_id, "name": name, "status": "creating",
            "vcpu": vcpu, "mem_mb": mem_mb, "disk_mb": disk_mb,
            "slot": slot, "ip_address": f"172.16.0.{slot}",
            "created_at": time.time(), "last_activity": time.time(),
            "pid": None, "snapshot": None,
            "agent_token": secrets.token_urlsafe(32),
        }
        _vm_state.create(vm_id, record)
    return record


async def _resolve_session_vm(session_id: str) -> str:
    """Return a running vm_id for the current MCP session, creating or resuming as needed."""
    log.info(f"_resolve_session_vm: session_id={repr(session_id)} map={_session_map._session}")

    vm_id = _session_map.get(session_id)
    if vm_id:
        record = _vm_state.get(vm_id)
        if record:
            if record["status"] == "running":
                return vm_id
            if record["status"] == "paused":
                log.info(f"Auto-resuming VM {vm_id} for session {session_id}")
                snap = record.get("snapshot")
                # If the local snapshot/overlay are gone (e.g. recovered onto a node
                # that lost its PV), pull them back from S3 before resuming.
                if (not snap or not Path(snap.get("state_path", "")).exists()
                        or not _overlay_path(vm_id).exists()):
                    if await _s3_restore_vm(vm_id):
                        record = _vm_state.get(vm_id)
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
                await _wait_for_agent(vm_id, timeout=30)
                if FS_FREEZE_ON_SNAPSHOT:
                    await _agent_fs_thaw(vm_id)
                _vm_state.update(vm_id, {"status": "running"})
                return vm_id

    # No usable VM found — create a new one
    log.info(f"Auto-creating VM for session {session_id}")
    new_vm_id = str(uuid.uuid4())
    name = f"vm-{new_vm_id[:8]}"

    await _allocate_and_create_record(new_vm_id, name, DEFAULT_VCPU, DEFAULT_MEM_MB, DEFAULT_DISK_MB)
    try:
        await _create_overlay(new_vm_id, DEFAULT_DISK_MB)
        pid = await _launch_firecracker(new_vm_id, DEFAULT_VCPU, DEFAULT_MEM_MB)
        _vm_state.update(new_vm_id, {"pid": pid, "status": "booting"})
        if not await _wait_for_agent(new_vm_id, timeout=45):
            raise RuntimeError("agent never became available after VM boot")
        _vm_state.update(new_vm_id, {"status": "running"})
    except Exception as e:
        _vm_state.update(new_vm_id, {"status": "error", "error": str(e)})
        raise

    _session_map.set(session_id, new_vm_id)
    return new_vm_id


async def reconcile_on_startup():
    """Reconcile persisted VM records against actual Firecracker processes on boot.

    A pod/process restart kills every child Firecracker PID but leaves overlays and
    snapshots on the (node-local) disk, so records that still say "running" are stale.
    This re-adopts genuinely live VMs, down-converts snapshot-bearing dead ones to
    "paused" (so _resolve_session_vm auto-resumes them), errors the unrecoverable
    rest, and rebuilds the slot free-list from the survivors. Flips _ready when done.
    """
    global _ready
    in_use_slots: List[int] = []
    for record in _vm_state.list_all():
        vm_id = record["vm_id"]
        pid = record.get("pid")
        socket = Path(_socket_path(vm_id))
        snap = record.get("snapshot")
        slot = _record_slot(record)

        if _pid_is_firecracker(pid) and socket.exists():
            _vm_state.update(vm_id, {"status": "running"})
            log.info(f"reconcile: re-adopted running VM {vm_id} (pid {pid}, slot {slot})")
        elif snap and Path(snap.get("state_path", "")).exists() and Path(snap.get("mem_path", "")).exists():
            _vm_state.update(vm_id, {"status": "paused", "pid": None})
            if socket.exists():
                socket.unlink()  # stale; resume relaunches a fresh FC process
            log.info(f"reconcile: VM {vm_id} -> paused (resumable from snapshot)")
        else:
            _vm_state.update(vm_id, {
                "status": "error", "pid": None,
                "error": "no live process and no snapshot after restart",
            })
            log.warning(f"reconcile: VM {vm_id} -> error (unrecoverable after restart)")

        # Keep the slot reserved for every surviving record (incl. error) so a new
        # VM never collides with one still holding an overlay/tap; destroy releases it.
        if slot is not None:
            in_use_slots.append(slot)

    _slots.rebuild_from_records(in_use_slots)
    _ready = True
    log.info(f"reconcile complete: {_slots.free_count()} free slots, node={NODE_NAME or '(standalone)'}")


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


class ExecInput(BaseModel):
    """Router-internal exec request. The router supplies the stable session id;
    this node resolves/creates/resumes that session's VM and runs the command."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., description="Stable Mcp-Session-Id from the router", min_length=1)
    command: str = Field(..., min_length=1, max_length=8192)
    timeout: int = Field(default=60, ge=1, le=600)
    working_dir: Optional[str] = Field(default=None)


class RestoreInput(BaseModel):
    """Router-internal: restore a VM from S3 onto this node and bind a session to it."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    vm_id: str = Field(..., min_length=1)
    session_id: Optional[str] = Field(default=None, description="Bind this session to the restored VM")


class VmExecInput(BaseModel):
    """Admin exec against a specific VM (used by fcctl). Unlike /exec, it does no session
    resolution and never auto-creates/resumes — the VM must already exist and be running."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    command: str = Field(..., min_length=1, max_length=8192)
    timeout: int = Field(default=60, ge=1, le=600)
    working_dir: Optional[str] = Field(default=None)


# ─── FastMCP (bash_exec only) ──────────────────────────────────────────────────

mcp = FastMCP(
    "firecracker_bash_mcp",
    # LOAD-BEARING: stateful + SSE. The session->VM resolution and (in the HA
    # topology) the router's session pinning both depend on the SDK assigning and
    # echoing Mcp-Session-Id. Flipping either of these silently breaks session
    # identity — treat a change here as a design fork, not a config tweak.
    stateless_http=False,
    json_response=False,
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
async def bash_exec(params: BashExecInput, ctx: Context) -> str:
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
        session_key = str(id(ctx.request_context.session))
        vm_id = params.vm_id or await _resolve_session_vm(session_key)
    except Exception as e:
        return json.dumps({"error": f"Could not resolve VM for session: {e}"})

    record = _vm_state.get(vm_id)
    if not record:
        return json.dumps({"error": f"VM '{vm_id}' not found."})
    if record["status"] == "paused":
        try:
            await api_vm_resume(vm_id)   # running a command auto-resumes a paused target VM
            record = _vm_state.get(vm_id)
        except Exception as e:
            return json.dumps({"error": f"Could not resume paused VM '{vm_id}': {e}"})
    if not record or record["status"] != "running":
        return json.dumps({
            "error": f"VM is not running (status: {record['status'] if record else 'missing'})."
        })

    _touch(vm_id)  # mark active so idle-pause won't reap it mid-use
    start = time.time()
    result = await _agent_exec(vm_id, params.command, timeout=params.timeout, working_dir=params.working_dir)
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

async def publish_nodeagent_loop():
    """Best-effort: publish this node-agent's identity + capacity as a NodeAgent CR.

    The router reads NodeAgent.status.freeTaps (the single capacity authority) and
    heartbeatTime (liveness). Skipped with a log line when not running in-cluster,
    so standalone dev keeps working.
    """
    try:
        from kubernetes_asyncio import client, config
    except Exception:
        log.info("kubernetes_asyncio unavailable; NodeAgent CR publishing disabled")
        return
    try:
        config.load_incluster_config()
    except Exception:
        log.info("not in-cluster; NodeAgent CR publishing disabled")
        return

    group, version, plural = "fcmcp.io", "v1alpha1", "nodeagents"
    namespace = os.environ.get("FC_MCP_NAMESPACE", "fc-mcp")
    # Use pod name (fc-node-agent-0/1/2) as the CR name so NodeAgent and pod are the same
    # identifier. NODE_NAME (the k8s node name, e.g. fc-mcp-worker3) is kept in spec so the
    # router can still correlate a NodeAgent to its underlying node if needed.
    pod_name = os.environ.get("POD_NAME") or NODE_NAME or "node-agent"
    pod_ip = os.environ.get("POD_IP", "")
    max_vms = int(os.environ.get("FC_MAX_VMS", str(SLOT_MAX - SLOT_MIN + 1)))
    merge = "application/merge-patch+json"

    co = client.CustomObjectsApi(client.ApiClient())
    body = {
        "apiVersion": f"{group}/{version}", "kind": "NodeAgent",
        "metadata": {"name": pod_name, "labels": {"fcmcp.io/node": pod_name, "fcmcp.io/k8s-node": NODE_NAME}},
        "spec": {"nodeName": NODE_NAME, "podName": pod_name, "podIP": pod_ip, "maxVms": max_vms},
    }
    try:
        await co.create_namespaced_custom_object(group, version, namespace, plural, body)
    except client.exceptions.ApiException as e:
        if e.status != 409:  # already exists is fine
            log.warning(f"could not create NodeAgent CR: {e}")

    while True:
        try:
            running = sum(1 for v in _vm_state.list_all() if v.get("status") == "running")
            phase = "Ready" if all(_readiness_checks().values()) else "NotReady"
            status_body = {"status": {
                "phase": phase,
                "freeTaps": _slots.free_count(),
                "runningVmCount": running,
                "heartbeatTime": datetime.now(timezone.utc).isoformat(),
            }}
            try:
                await co.patch_namespaced_custom_object_status(
                    group, version, namespace, plural, pod_name, status_body, _content_type=merge)
            except TypeError:
                await co.patch_namespaced_custom_object_status(
                    group, version, namespace, plural, pod_name, status_body)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"NodeAgent heartbeat failed: {e}")
        await asyncio.sleep(10)


async def idle_pause_loop():
    """Pause VMs idle longer than IDLE_PAUSE_SECONDS (snapshot + free RAM/CPU).

    The next bash_exec auto-resumes them via _resolve_session_vm. Reuses the REST
    pause path. Disabled when IDLE_PAUSE_SECONDS <= 0.
    """
    if IDLE_PAUSE_SECONDS <= 0:
        log.info("idle-pause disabled (FC_IDLE_PAUSE_SECONDS<=0)")
        return
    log.info(f"idle-pause enabled: pause after {IDLE_PAUSE_SECONDS}s idle (check every {IDLE_CHECK_INTERVAL}s)")
    while True:
        try:
            await asyncio.sleep(IDLE_CHECK_INTERVAL)
            now = time.time()
            for v in list(_vm_state.list_all()):
                if v.get("status") != "running":
                    continue
                idle = now - (v.get("last_activity") or v.get("created_at") or now)
                if idle >= IDLE_PAUSE_SECONDS:
                    vm_id = v["vm_id"]
                    log.info(f"idle-pause: pausing {vm_id} (idle {int(idle)}s)")
                    try:
                        await _pause_vm(vm_id, archive=True)
                    except Exception as e:
                        log.warning(f"idle-pause failed for {vm_id}: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"idle-pause loop error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    for d in [BASE_DIR, VM_IMAGES_DIR, SNAPSHOTS_DIR, SOCKETS_DIR, BASE_DIR / "overlays"]:
        d.mkdir(parents=True, exist_ok=True)
    log.info(f"Firecracker server started. Base dir: {BASE_DIR}")
    await reconcile_on_startup()
    na_task = asyncio.create_task(publish_nodeagent_loop())
    idle_task = asyncio.create_task(idle_pause_loop())
    # Run MCP session manager alongside FastAPI
    async with mcp._session_manager.run():
        try:
            yield
        finally:
            na_task.cancel()
            idle_task.cancel()


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

            # Log full request body for initialize requests to find stable identifiers
            body_chunks = []
            async def receive_logging():
                msg = await receive()
                if msg["type"] == "http.request":
                    body_chunks.append(msg.get("body", b""))
                    try:
                        parsed = json.loads(b"".join(body_chunks))
                        if parsed.get("method") == "initialize":
                            log.info(f"MCP initialize body: {json.dumps(parsed)}")
                            log.info(f"MCP initialize headers: { {k.decode():v.decode() for k,v in scope.get('headers',[])} }")
                    except Exception:
                        pass
                return msg

            scope["headers"] = [
                (k, v) for k, v in scope.get("headers", []) if k.lower() != b"host"
            ] + [(b"host", b"localhost")]
            await self.mcp_handler(scope, receive_logging, send)
        else:
            await self.api_app(scope, receive, send)


# ─── REST Endpoints ────────────────────────────────────────────────────────────

def _bridge_up() -> bool:
    """Whether fc-br0 exists. On non-Linux (dev) there is no /sys/class/net; don't gate on it."""
    sysnet = Path("/sys/class/net")
    if not sysnet.exists():
        return True
    return (sysnet / "fc-br0").exists()


@api.get("/health", tags=["Health"], summary="Liveness probe")
async def api_health():
    """Liveness: the process is up and serving. Always 200 while the event loop runs."""
    return {"status": "ok", "node": NODE_NAME or None}


def _readiness_checks() -> Dict[str, bool]:
    return {
        "reconciled": _ready,
        "kernel_image": KERNEL_IMAGE.exists(),
        "base_rootfs": BASE_ROOTFS.exists(),
        "bridge_up": _bridge_up(),
        "free_slots": _slots.free_count() > 0,
    }


@api.get("/ready", tags=["Health"], summary="Readiness probe")
async def api_ready():
    """Readiness: this node can successfully serve a new VM.

    Returns 503 (so the scheduler/Service stops sending new work here) when reconcile
    hasn't finished, images/key are missing, the bridge is down, or all slots are full —
    while liveness stays green so existing pinned VMs keep running.
    """
    checks = _readiness_checks()
    ok = all(checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"ready": ok, "free_slots": _slots.free_count(), "checks": checks},
    )


@api.post("/drain", tags=["Health"], summary="Pause+snapshot every running VM (preStop hook)")
async def api_drain():
    """Snapshot all running VMs to the node-local PV so a pod restart/upgrade does not
    kill in-VM state. Invoked by the StatefulSet preStop hook; reconcile + the router's
    next exec then auto-resume each VM from its snapshot."""
    results = []
    for v in list(_vm_state.list_all()):
        if v.get("status") == "running":
            try:
                # archive=False: preStop = same-node pod restart; the local PV persists,
                # so S3 isn't needed and uploading every VM within the grace window is risky.
                await _pause_vm(v["vm_id"], archive=False)
                results.append({"vm_id": v["vm_id"], "paused": True})
            except Exception as e:
                results.append({"vm_id": v["vm_id"], "paused": False, "error": str(e)})
    return {"drained": results}


@api.post("/exec", tags=["Exec"], summary="Run a command in the session's VM (router-internal)")
async def api_exec(params: ExecInput):
    """Resolve (create/resume) this session's VM on this node and run the command.

    The router calls this over the internal network after pinning the session here;
    the body mirrors what the MCP bash_exec tool used to do, keyed on the stable
    session id instead of an in-process object id.
    """
    try:
        vm_id = await _resolve_session_vm(params.session_id)
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": f"could not resolve VM: {e}"})

    record = _vm_state.get(vm_id)
    if not record or record["status"] != "running":
        status = record["status"] if record else "missing"
        return JSONResponse(status_code=409, content={"error": f"VM not running (status: {status})"})

    _touch(vm_id)  # mark active so idle-pause won't reap it mid-use
    start = time.time()
    result = await _agent_exec(vm_id, params.command, timeout=params.timeout, working_dir=params.working_dir)
    elapsed = round(time.time() - start, 2)
    return {
        "vm_id": vm_id, "command": params.command,
        "stdout": result["stdout"], "stderr": result["stderr"],
        "returncode": result["returncode"], "elapsed_seconds": elapsed,
    }


@api.post("/restore", tags=["Exec"], summary="Restore a VM from S3 + bind a session (router-internal)")
async def api_restore(params: RestoreInput):
    """Recover a paused VM that was archived to S3 (e.g. after node loss): downloads its
    artifacts onto this node, reserves its slot, resumes it, and binds the session so the
    next /exec reaches it. api_vm_resume does the S3 pull-on-missing."""
    if not S3_ENABLED:
        return JSONResponse(status_code=503, content={"error": "S3 archival disabled (FC_S3_BUCKET unset)"})
    try:
        result = await api_vm_resume(params.vm_id)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"error": e.detail})
    if params.session_id:
        _session_map.set(params.session_id, params.vm_id)
    return {"vm_id": params.vm_id, "session_id": params.session_id, "status": "running", "resume": result}


@api.post("/vms", status_code=201, tags=["VMs"], summary="Create a new microVM")
async def api_vm_create(params: VMCreateInput):
    """Create and boot a new Firecracker microVM. Returns a `vm_id` to use with `bash_exec`."""
    vm_id = str(uuid.uuid4())
    name = params.name or f"vm-{vm_id[:8]}"
    try:
        await _allocate_and_create_record(vm_id, name, params.vcpu, params.mem_mb, params.disk_mb)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        await _create_overlay(vm_id, params.disk_mb)
        pid = await _launch_firecracker(vm_id, params.vcpu, params.mem_mb)
        _vm_state.update(vm_id, {"pid": pid, "status": "booting"})
        ready = await _wait_for_agent(vm_id, timeout=45)
        if not ready:
            _vm_state.update(vm_id, {"status": "error", "error": "agent timeout"})
            raise HTTPException(status_code=500, detail="VM booted but the agent never became available.")
        _vm_state.update(vm_id, {"status": "running"})
        return {
            "vm_id": vm_id, "name": name, "status": "running",
            "ip_address": _vm_ip(vm_id),
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
                "ip_address": v.get("ip_address"),
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
        "disk_mb": record.get("disk_mb"), "ip_address": record.get("ip_address"),
        "pid": record.get("pid"), "created_at": record.get("created_at"),
        "snapshot": snap_info,
        "error": record.get("error"),
    }


@api.post("/vms/{vm_id}/exec", tags=["VMs"], summary="Run a command in a VM (admin)")
async def api_vm_exec(vm_id: str, params: VmExecInput):
    """Run a command in a specific running VM, addressed by id (the fcctl control plane).

    Mirrors bash_exec minus session resolution: 404 if the VM is unknown; a paused VM is
    auto-resumed first; 409 if it is otherwise not running (no auto-create). Admin-only by
    network position, like the rest of /vms/*.
    """
    record = _vm_state.get(vm_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"VM '{vm_id}' not found.")
    if record["status"] == "paused":
        await api_vm_resume(vm_id)   # running a command auto-resumes a paused target VM
        record = _vm_state.get(vm_id)
    if not record or record["status"] != "running":
        raise HTTPException(status_code=409, detail=f"VM is not running (status: {record['status'] if record else 'missing'}).")
    _touch(vm_id)
    start = time.time()
    result = await _agent_exec(vm_id, params.command, timeout=params.timeout, working_dir=params.working_dir)
    return {
        "vm_id": vm_id, "command": params.command,
        "stdout": result["stdout"], "stderr": result["stderr"],
        "returncode": result["returncode"],
        "elapsed_seconds": round(time.time() - start, 2),
    }


async def _pause_vm(vm_id: str, archive: bool = True) -> Dict:
    """Snapshot + pause a VM (kill its FC process). If archive and S3 is enabled, also
    upload the snapshot to S3 so it survives node loss. Raises KeyError (not found) or
    RuntimeError (not running / pause failed)."""
    record = _vm_state.get(vm_id)
    if not record:
        raise KeyError(f"VM '{vm_id}' not found.")
    if record["status"] != "running":
        raise RuntimeError(f"VM must be running to pause (status: {record['status']}).")

    snap_dir = _snapshot_dir(vm_id)
    snap_dir.mkdir(parents=True, exist_ok=True)
    mem_path = str(snap_dir / "memory.bin")
    state_path = str(snap_dir / "vmstate.bin")

    try:
        fc = FirecrackerClient(_socket_path(vm_id))
        if FS_FREEZE_ON_SNAPSHOT:
            # Freeze before pausing so the captured overlay is fs-consistent. The
            # snapshot's *memory* image is therefore frozen too → resume must thaw
            # (see _resolve_session_vm / api_vm_resume). The VM is killed right after,
            # so no thaw is needed here; the agent's watchdog covers a crash before kill.
            await _agent_fs_freeze(vm_id)
        await fc.patch("/vm", {"state": "Paused"})
        await fc.put("/snapshot/create", {
            "snapshot_type": "Full", "snapshot_path": state_path, "mem_file_path": mem_path,
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
        _vm_state.update(vm_id, {
            "status": "paused", "pid": None,
            "snapshot": {"created_at": time.time(), "mem_path": mem_path, "state_path": state_path},
        })
    except Exception as e:
        raise RuntimeError(f"Failed to pause VM: {e}")

    if archive:
        await _s3_archive_vm(vm_id)
    mem_size_mb = round(Path(mem_path).stat().st_size / 1024 / 1024, 1) if Path(mem_path).exists() else 0
    return {"vm_id": vm_id, "status": "paused", "mem_snapshot_mb": mem_size_mb}


@api.post("/vms/{vm_id}/pause", tags=["VMs"], summary="Pause VM and save snapshot")
async def api_vm_pause(vm_id: str):
    try:
        return await _pause_vm(vm_id, archive=True)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409 if "must be running" in str(e) else 500, detail=str(e))


@api.post("/vms/{vm_id}/resume", tags=["VMs"], summary="Resume VM from snapshot")
async def api_vm_resume(vm_id: str):
    record = _vm_state.get(vm_id)
    # Unknown locally or files gone (e.g. recovered onto a node that lost its PV):
    # pull the snapshot back from S3, which rebuilds the record as 'paused'.
    if not record or record.get("status") != "paused" or not Path((record.get("snapshot") or {}).get("state_path", "")).exists():
        if await _s3_restore_vm(vm_id):
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

        ready = await _wait_for_agent(vm_id, timeout=30)
        if FS_FREEZE_ON_SNAPSHOT:
            await _agent_fs_thaw(vm_id)
        _vm_state.update(vm_id, {"status": "running", "pid": fc_proc.pid})

        return {
            "vm_id": vm_id, "name": record["name"], "status": "running", "agent_ready": ready,
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

    _slots.release(_record_slot(record))
    _vm_state.delete(vm_id)
    _session_map.remove(vm_id)
    await _s3_delete_vm(vm_id)  # no-op when S3 disabled; removes the archive otherwise
    _rebuild_egress_index()  # drop this VM's ip->policy entry so a reused slot can't inherit it
    return {"vm_id": vm_id, "name": record["name"], "status": "destroyed"}


# ─── Agent sessions (Claude Agent SDK in-VM, managed-agents-shaped) ─────────────
#
# Agent  = a stored definition {model, system, allowed_tools} (managed-agents Agent).
# Session = a VM running the in-VM SDK runner from an Agent's definition; driven by
#           appending user messages to the runner's input file and streaming its events.
# This rides the existing fc-agent /exec_async reattachable path and _resolve_session_vm.

class _JsonStore:
    """Tiny JSON-dict persistence (same pattern as VMState) for agents / agent-sessions."""
    def __init__(self, path: Path):
        self._path = path
        try:
            self._d: Dict[str, Dict] = json.loads(path.read_text())
        except Exception:
            self._d = {}

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._d, indent=2))
        tmp.replace(self._path)

    def get(self, k): return self._d.get(k)
    def put(self, k, v): self._d[k] = v; self._save()
    def delete(self, k): self._d.pop(k, None); self._save()
    def all(self): return list(self._d.values())


AGENTS = _JsonStore(BASE_DIR / "agents.json")
ENVIRONMENTS = _JsonStore(BASE_DIR / "environments.json")
AGENT_SESSIONS = _JsonStore(BASE_DIR / "agent-sessions.json")
DEFAULT_AGENT_MODEL = os.environ.get("FC_AGENT_MODEL", "claude-sonnet-4-6")

# --- Egress broker (fc-egress) policy index ---------------------------------------
# The transparent egress proxy enforces per-session egress policy and injects credentials
# *after* traffic leaves the VM (the VM holds no secrets). The proxy never queries us at
# runtime: we denormalize a {vm-ip: egress_policy} map to this file on the shared PV and the
# proxy mmaps/polls it (see egress/policy.go). Gated by FC_EGRESS_ENABLED.
EGRESS_INDEX_PATH = BASE_DIR / "egress-index.json"
FC_EGRESS_ENABLED = os.environ.get("FC_EGRESS_ENABLED", "").lower() not in ("", "0", "false", "no")
FC_EGRESS_CA_DIR = Path(os.environ.get("FC_EGRESS_CA_DIR", str(BASE_DIR / "egress-ca")))


def _repo_slug(repository_url: str) -> str:
    """Normalize a repo reference (owner/repo shorthand, https URL, or git@ ssh) to 'owner/repo'."""
    u = repository_url.strip()
    if u.startswith("git@") and ":" in u:
        u = u.split(":", 1)[1]
    elif "://" in u:
        rest = u.split("://", 1)[1]
        u = rest.split("/", 1)[1] if "/" in rest else ""
    u = u.strip("/")
    if u.endswith(".git"):
        u = u[:-4]
    segs = [s for s in u.split("/") if s]
    return f"{segs[-2]}/{segs[-1]}" if len(segs) >= 2 else u


def _derive_egress_policy(session_id: str, resources, explicit: Optional[Dict]) -> Optional[Dict]:
    """Build a session's egress_policy from its github_repository resources, merged with any
    explicit policy. Returns None when there is nothing to permit (default-deny everywhere)."""
    repos: List[str] = []
    hosts = set()
    for r in (resources or []):
        if (r or {}).get("type") == "github_repository":
            repos.append(_repo_slug(r["repository_url"]))
            hosts.update(("github.com", "*.githubusercontent.com"))
    if explicit:
        hosts.update(explicit.get("allowed_hosts") or [])
        gh = explicit.get("github") or {}
        repos.extend(gh.get("repos") or [])
    if not repos and not hosts and not explicit:
        return None
    deduped = list(dict.fromkeys(repos))
    pol: Dict[str, Any] = {"session_id": session_id, "mode": "default_deny",
                           "allowed_hosts": sorted(hosts)}
    if deduped:
        pol["github"] = {"repos": deduped}
    return pol


def _ip_of_record(record: Dict) -> Optional[str]:
    if record.get("ip_address"):
        return record["ip_address"]
    slot = _record_slot(record)
    return f"172.16.0.{slot}" if slot is not None else None


def _build_egress_index(vm_records: List[Dict], sessions: List[Dict]) -> Dict[str, Dict]:
    """Join each session's egress_policy to its VM's IP -> {ip: policy}. Sessions without a
    policy, or whose VM isn't present, are omitted (so the proxy default-denies them)."""
    ip_by_vm = {}
    for v in vm_records:
        ip = _ip_of_record(v)
        if ip:
            ip_by_vm[v.get("vm_id")] = ip
    idx: Dict[str, Dict] = {}
    for s in sessions:
        pol = s.get("egress_policy")
        ip = ip_by_vm.get(s.get("vm_id"))
        if pol and ip:
            idx[ip] = pol
    return idx


def _rebuild_egress_index() -> None:
    """Atomically rewrite egress-index.json from current VM + session state. Called on every
    lifecycle change that affects the mapping (session create/terminate, policy edit, VM
    create/destroy/pause-resume). Best-effort: a failure here must never break the operation."""
    try:
        idx = _build_egress_index(_vm_state.list_all(), AGENT_SESSIONS.all())
        EGRESS_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = EGRESS_INDEX_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(idx, indent=2))
        tmp.replace(EGRESS_INDEX_PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("egress index rebuild failed: %s", e)

# Map managed-agents-style toolset names (agent_toolset_20260401: bash/edit/read/…) onto the
# Claude Agent SDK's tool names. Done host-side (here) so the runner always receives SDK names
# and needs no rebuild when the mapping changes. Already-correct SDK names pass through.
# NOTE (deferred): custom tools (agent.custom_tool_use → idle → user.custom_tool_result) are a
# separate round-trip that needs runner support; not implemented here.
_TOOL_NAME_MAP = {
    "bash": "Bash", "edit": "Edit", "read": "Read", "write": "Write",
    "glob": "Glob", "grep": "Grep", "web_fetch": "WebFetch", "web_search": "WebSearch",
}


def _normalize_tools(tools: Optional[List[str]]) -> Optional[List[str]]:
    """Normalize a tool list to SDK names. Accepts managed-agents names (bash, web_fetch, …)
    or SDK names (Bash, WebFetch, …); unknown names pass through unchanged (e.g. mcp__… )."""
    if not tools:
        return tools
    return [_TOOL_NAME_MAP.get(t, _TOOL_NAME_MAP.get(t.lower(), t)) for t in tools]


def _agent_snapshot(agent: Dict, version: Optional[int] = None) -> Dict:
    """Resolve a frozen, immutable config snapshot for an agent version (default: latest).
    Versions are stored in agent['versions'] keyed by stringified version number."""
    versions = agent.get("versions") or {}
    if version is None:
        version = agent.get("version", 1)
    snap = versions.get(str(version))
    if snap is None:  # legacy agent (pre-versioning) — synthesize from top-level fields
        snap = {"version": agent.get("version", 1), "name": agent.get("name"),
                "model": agent.get("model"), "system": agent.get("system"),
                "allowed_tools": agent.get("allowed_tools")}
    return snap


def _agent_headers(vm_id: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {(_vm_state.get(vm_id) or {}).get('agent_token', '')}"}


async def _agent_runner_start(vm_id: str, sid: str) -> Dict[str, Any]:
    """Start the persistent in-VM Claude agent runner for a session via the typed
    fc-agent /agent/start endpoint (idempotent / reconnect-safe)."""
    url = f"http://{_vm_ip(vm_id)}:{AGENT_HTTP_PORT}/agent/start"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, json={"session_id": sid}, headers=_agent_headers(vm_id))
    r.raise_for_status()
    return r.json()


async def _agent_runner_input(vm_id: str, sid: str, content: str) -> None:
    """Append a user message (drive a turn) via fc-agent /agent/input — fc-agent JSON-encodes
    it onto the runner's input file, so no host-side base64/quoting is needed."""
    url = f"http://{_vm_ip(vm_id)}:{AGENT_HTTP_PORT}/agent/input"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, json={"session_id": sid, "content": content},
                              headers=_agent_headers(vm_id))
    r.raise_for_status()


async def _agent_runner_events(vm_id: str, sid: str, from_off: int) -> Dict[str, Any]:
    """Drain the runner's stream-json events from a byte offset via fc-agent /agent/events.
    Returns {stdout, offset, done, returncode}."""
    url = f"http://{_vm_ip(vm_id)}:{AGENT_HTTP_PORT}/agent/events"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url, params={"session_id": sid, "from": from_off},
                             headers=_agent_headers(vm_id))
    r.raise_for_status()
    return r.json()


async def _agent_runner_stop(vm_id: str, sid: str) -> None:
    """Stop the runner (graceful stop hint + kill + cleanup) via fc-agent DELETE /agent/{sid}."""
    url = f"http://{_vm_ip(vm_id)}:{AGENT_HTTP_PORT}/agent/{sid}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        await client.delete(url, headers=_agent_headers(vm_id))


async def _agent_b64write(vm_id: str, path: str, content: str, append: bool = False):
    """Write/append content to a guest file via base64 (quoting-safe; the value rides the
    fc-agent exec body, not the visible command). Raises on failure."""
    b64 = base64.b64encode(content.encode()).decode()
    parent = path.rsplit("/", 1)[0]
    redir = ">>" if append else ">"
    res = await _agent_exec(vm_id, f"mkdir -p {parent} && echo {b64} | base64 -d {redir} {path}")
    if res.get("returncode") != 0:
        raise RuntimeError(f"guest write to {path} failed: {res.get('stderr')}")


async def _create_and_boot_vm(name: str, vcpu: int, mem_mb: int, disk_mb: int) -> str:
    """Create + boot a VM with explicit specs (mirrors api_vm_create's inner flow)."""
    vm_id = str(uuid.uuid4())
    await _allocate_and_create_record(vm_id, name, vcpu, mem_mb, disk_mb)
    try:
        await _create_overlay(vm_id, disk_mb)
        pid = await _launch_firecracker(vm_id, vcpu, mem_mb)
        _vm_state.update(vm_id, {"pid": pid, "status": "booting"})
        if not await _wait_for_agent(vm_id, timeout=45):
            raise RuntimeError("agent never became available after VM boot")
        _vm_state.update(vm_id, {"status": "running"})
    except Exception as e:
        _vm_state.update(vm_id, {"status": "error", "error": str(e)})
        raise
    return vm_id


async def _provision_resources(vm_id: str, resources: List[Dict]) -> List[Dict]:
    """Materialize session resources into the VM before the agent runs. Returns sanitized
    resource records to store on the session — the authorization_token is NEVER returned or
    persisted. A git token is injected via a short-lived credential file written over the
    fc-agent body (base64, not in the visible command) and shredded right after the clone."""
    out: List[Dict] = []
    for r in resources:
        rtype = r.get("type")
        if rtype == "github_repository":
            url = r["repository_url"]
            if not url.startswith(("http://", "https://", "git@")):
                url = f"https://github.com/{url}"  # owner/repo shorthand
            repo_name = url.rstrip("/").split("/")[-1]
            if repo_name.endswith(".git"):
                repo_name = repo_name[:-4]
            target = r.get("target_dir") or f"/workspace/{repo_name}"
            # With the egress broker on, the VM holds NO secret: clone tokenlessly and let
            # fc-egress inject a repo-scoped token after the request leaves the VM. Otherwise
            # fall back to the legacy short-lived .git-credentials file (written, used, shredded).
            token = None if FC_EGRESS_ENABLED else r.get("authorization_token")
            branch_arg = f"-b {shlex.quote(r['branch'])} " if r.get("branch") else ""
            if token:
                host = urlparse(url).hostname or "github.com"
                await _agent_b64write(vm_id, "/root/.git-credentials",
                                      f"https://x-access-token:{token}@{host}\n")
            clone = (f"mkdir -p /workspace && GIT_TERMINAL_PROMPT=0 HOME=/root "
                     f"git -c credential.helper=store "
                     f"clone --depth 1 {branch_arg}{shlex.quote(url)} {shlex.quote(target)}")
            cmd = clone + ("; rc=$?; shred -u /root/.git-credentials 2>/dev/null || "
                           "rm -f /root/.git-credentials; exit $rc" if token else "")
            res = await _agent_exec(vm_id, cmd, timeout=300)
            if res.get("returncode") != 0:
                raise RuntimeError(f"git clone failed for {url}: {(res.get('stderr') or '')[:300]}")
            out.append({"type": "github_repository", "repository_url": url,
                        "target_dir": target, "branch": r.get("branch")})
        elif rtype == "file":
            await _agent_b64write(vm_id, r["path"], r.get("content", ""))
            out.append({"type": "file", "path": r["path"]})
    return out


def _zero_usage() -> Dict:
    return {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0, "total_cost_usd": 0.0, "turns": 0}


def _usage_from_result(ev: Dict) -> Dict:
    """Pull this turn's usage out of an SDK 'result' event."""
    u = ev.get("usage") or {}
    return {"input_tokens": int(u.get("input_tokens", 0) or 0),
            "output_tokens": int(u.get("output_tokens", 0) or 0),
            "cache_read_input_tokens": int(u.get("cache_read_input_tokens", 0) or 0),
            "cache_creation_input_tokens": int(u.get("cache_creation_input_tokens", 0) or 0),
            "total_cost_usd": float(ev.get("total_cost_usd", 0.0) or 0.0)}


async def _refresh_session_usage(sid: str) -> Dict:
    """Recompute absolute session usage by summing every 'result' event in the runner's output
    (idempotent — never double-counts across re-streams). If the VM can't be polled (e.g. it's
    paused) and we'd otherwise regress to zero, the last-known usage is kept."""
    rec = AGENT_SESSIONS.get(sid)
    if not rec:
        raise KeyError(f"session '{sid}' not found")
    events = await _replay_events(sid)
    total = _zero_usage()
    for ev in events:
        if ev.get("type") == "result":
            u = _usage_from_result(ev)
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                      "cache_creation_input_tokens"):
                total[k] += u[k]
            total["total_cost_usd"] += u["total_cost_usd"]
            total["turns"] += 1
    prev = rec.get("usage") or _zero_usage()
    if total["turns"] == 0 and prev.get("turns", 0) > 0:
        return prev  # couldn't reach the VM; don't clobber a real total with zeros
    rec["usage"] = total
    rec["updated_at"] = time.time()
    AGENT_SESSIONS.put(sid, rec)
    return total


# NOTE (deferred): managed-agents outcomes (user.define_outcome + outcome_evaluations) are not
# implemented. They need a runner round-trip to score a session against declared success criteria;
# tracked for a later phase alongside the custom-tool round-trip.

async def _create_agent_session(agent_id: Optional[str] = None, title: Optional[str] = None,
                                environment_id: Optional[str] = None,
                                agent_version: Optional[int] = None,
                                resources: Optional[List[Dict]] = None,
                                agent: Optional[Dict] = None,
                                environment: Optional[Dict] = None,
                                egress_policy: Optional[Dict] = None) -> Dict:
    # agent/environment may be passed inline by the router (HA); otherwise resolved locally.
    agent = agent or (AGENTS.get(agent_id) if agent_id else None)
    if not agent:
        raise KeyError(f"agent '{agent_id}' not found")
    agent_id = agent.get("id", agent_id)
    if agent.get("archived_at"):
        raise ValueError(f"agent '{agent_id}' is archived")
    snap = _agent_snapshot(agent, agent_version)
    if agent_version is not None and not (agent.get("versions") or {}).get(str(agent_version)):
        raise KeyError(f"agent '{agent_id}' has no version {agent_version}")
    env = environment
    if env is None and environment_id:
        env = ENVIRONMENTS.get(environment_id)
        if not env:
            raise KeyError(f"environment '{environment_id}' not found")
    sid = "sesn_" + secrets.token_hex(12)
    vm_id = await _create_and_boot_vm(
        f"agent-{sid[5:17]}",
        (env or {}).get("vcpu", DEFAULT_VCPU),
        (env or {}).get("mem_mb", DEFAULT_MEM_MB),
        (env or {}).get("disk_mb", DEFAULT_DISK_MB),
    )
    _session_map.set(sid, vm_id)  # bind so later turns auto-resume the same VM
    policy = _derive_egress_policy(sid, resources, egress_policy)
    rec = {"id": sid, "type": "session", "agent_id": agent_id,
           "agent_version": snap.get("version"), "agent_snapshot": snap,
           "environment_id": environment_id, "vm_id": vm_id, "status": "provisioning",
           "title": title, "resources": [], "egress_policy": policy,
           "usage": _zero_usage(), "events_offset": 0,
           "created_at": time.time(), "updated_at": time.time()}
    # Persist the policy + VM binding and publish the egress index BEFORE provisioning, so the
    # broker can resolve this VM's policy when the (secretless) clone runs.
    AGENT_SESSIONS.put(sid, rec)
    _rebuild_egress_index()
    cfg: Dict[str, Any] = {"model": snap.get("model") or DEFAULT_AGENT_MODEL}
    if snap.get("system"):
        cfg["system"] = snap["system"]
    tools = _normalize_tools(snap.get("allowed_tools"))
    if tools:
        cfg["allowedTools"] = tools
    await _agent_b64write(vm_id, "/etc/fc-agent-runner/agent.json", json.dumps(cfg))
    provisioned = await _provision_resources(vm_id, resources or [])  # clone repos / drop files first
    await _agent_runner_start(vm_id, sid)  # typed /agent/start; events keyed by sid
    rec["resources"] = provisioned
    rec["status"] = "idle"
    rec["updated_at"] = time.time()
    AGENT_SESSIONS.put(sid, rec)
    return rec


async def _session_send(sid: str, content: str) -> Dict:
    rec = AGENT_SESSIONS.get(sid)
    if not rec:
        raise KeyError(f"session '{sid}' not found")
    _touch(rec["vm_id"])  # keep the VM alive while a turn runs
    await _agent_runner_input(rec["vm_id"], sid, content)  # typed /agent/input
    rec["status"] = "running"; rec["updated_at"] = time.time()
    AGENT_SESSIONS.put(sid, rec)
    return rec


async def _drive_turn(sid: str, content: str, timeout: float = 300.0) -> Dict:
    """Send a message and block until that turn's 'result' event, returning {result, events}.
    Tracks the session's consumed byte offset (events_offset) so multi-turn sessions resume
    from the right place instead of replaying a prior turn's result."""
    rec = AGENT_SESSIONS.get(sid)
    if not rec:
        raise KeyError(f"session '{sid}' not found")
    start_off = int(rec.get("events_offset", 0))
    await _session_send(sid, content)
    vm_id = rec["vm_id"]
    out_off, buf, last, result, n = start_off, "", time.time(), None, 0
    while time.time() - last < timeout:
        try:
            d = await _agent_runner_events(vm_id, sid, out_off)
        except Exception:
            await asyncio.sleep(0.4)
            continue
        chunk = d.get("stdout", "")
        out_off = d.get("offset", out_off)
        done_turn = False
        if chunk:
            last = time.time()
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                n += 1
                if ev.get("type") == "result":
                    result = ev.get("result")
                    done_turn = True
                    break
        if done_turn or d.get("done"):
            break
        await asyncio.sleep(0.3)
    r2 = AGENT_SESSIONS.get(sid)
    if r2:
        r2["events_offset"] = out_off
        r2["status"] = "idle"
        r2["updated_at"] = time.time()
        AGENT_SESSIONS.put(sid, r2)
    return {"result": result, "events": n, "offset": out_off}


async def _stream_session_events(sid: str, from_off: int = 0, max_idle: float = 300.0):
    """Async-generator of the session runner's stream-json events, drained from fc-agent by
    byte offset. Buffers partial lines across 1 MiB poll chunks."""
    rec = AGENT_SESSIONS.get(sid)
    if not rec:
        return
    vm_id = rec["vm_id"]
    out_off, buf, last = from_off, "", time.time()
    while time.time() - last < max_idle:
        try:
            d = await _agent_runner_events(vm_id, sid, out_off)
        except Exception:
            await asyncio.sleep(0.4)
            continue
        chunk = d.get("stdout", "")
        out_off = d.get("offset", out_off)
        if chunk:
            last = time.time()
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if line.strip():
                    try:
                        yield json.loads(line)
                    except Exception:
                        pass
        if d.get("done"):
            r2 = AGENT_SESSIONS.get(sid)
            if r2:
                r2["status"] = "terminated"; AGENT_SESSIONS.put(sid, r2)
            return
        await asyncio.sleep(0.3)


class AgentCreateInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(..., min_length=1, max_length=128)
    model: Optional[str] = None
    system: Optional[str] = Field(default=None, max_length=20000)
    allowed_tools: Optional[List[str]] = None


class AgentUpdateInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    model: Optional[str] = None
    system: Optional[str] = Field(default=None, max_length=20000)
    allowed_tools: Optional[List[str]] = None


class GithubRepoResource(BaseModel):
    """Clone a git repo into the session VM at create time (managed-agents github_repository)."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    type: Literal["github_repository"]
    repository_url: str = Field(..., min_length=1, max_length=1024)  # https URL or owner/repo
    authorization_token: Optional[str] = Field(default=None, max_length=500)  # PAT; never stored
    branch: Optional[str] = Field(default=None, max_length=255)
    target_dir: Optional[str] = Field(default=None, max_length=512)  # default /workspace/<repo>


class FileResource(BaseModel):
    """Write an inline file into the session VM at create time (managed-agents file)."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    type: Literal["file"]
    path: str = Field(..., min_length=1, max_length=512)
    content: str = Field(default="", max_length=1_000_000)


ResourceInput = Annotated[Union[GithubRepoResource, FileResource], Field(discriminator="type")]


class GitHubEgressInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repos: List[str] = Field(default_factory=list, max_length=200)  # "owner/repo" or "owner/*"


class EgressPolicyInput(BaseModel):
    """Per-session egress allowances. Merged over what's derived from github_repository
    resources; enforced by the fc-egress broker (the VM itself holds no credentials)."""
    model_config = ConfigDict(extra="forbid")
    allowed_hosts: List[str] = Field(default_factory=list, max_length=500)  # exact or "*.suffix"
    github: Optional[GitHubEgressInput] = None


class SessionCreateInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: Optional[str] = Field(default=None, min_length=1)
    agent_version: Optional[int] = Field(default=None, ge=1)
    environment_id: Optional[str] = None
    title: Optional[str] = Field(default=None, max_length=200)
    resources: Optional[List[ResourceInput]] = None
    egress_policy: Optional[EgressPolicyInput] = None  # broker policy (merged with resource-derived)
    # Router-internal (HA): the router resolves the agent/environment definitions on whatever
    # node owns them and passes them inline, so a session can run on any node regardless of
    # where its agent/env were created. Ignored in the standalone agent_id flow.
    agent: Optional[Dict[str, Any]] = None
    environment: Optional[Dict[str, Any]] = None


class SessionUpdateInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    title: Optional[str] = Field(default=None, max_length=200)


class EnvironmentCreateInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(..., min_length=1, max_length=128)
    vcpu: int = Field(default=DEFAULT_VCPU, ge=1, le=8)
    mem_mb: int = Field(default=DEFAULT_MEM_MB, ge=128, le=8192)
    disk_mb: int = Field(default=DEFAULT_DISK_MB, ge=512, le=20480)


class SessionMessageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    content: str = Field(..., min_length=1, max_length=100000)


class AgentRunInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1, max_length=100000)
    timeout: int = Field(default=300, ge=1, le=3600)


@api.post("/v1/agents", status_code=201, tags=["Agents"], summary="Create an agent definition")
async def api_agent_create(params: AgentCreateInput):
    aid = "agent_" + secrets.token_hex(12)
    now = time.time()
    model = params.model or DEFAULT_AGENT_MODEL
    snap = {"version": 1, "name": params.name, "model": model,
            "system": params.system, "allowed_tools": params.allowed_tools, "created_at": now}
    rec = {"id": aid, "type": "agent", "version": 1, "name": params.name,
           "model": model, "system": params.system, "allowed_tools": params.allowed_tools,
           "versions": {"1": snap}, "archived_at": None,
           "created_at": now, "updated_at": now}
    AGENTS.put(aid, rec)
    return rec


@api.get("/v1/agents", tags=["Agents"], summary="List agents")
async def api_agent_list():
    return {"data": AGENTS.all()}


@api.get("/v1/agents/{agent_id}", tags=["Agents"], summary="Get an agent")
async def api_agent_get(agent_id: str):
    a = AGENTS.get(agent_id)
    if not a:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' not found")
    return a


@api.post("/v1/agents/{agent_id}", tags=["Agents"], summary="Update an agent (creates a new version)")
async def api_agent_update(agent_id: str, params: AgentUpdateInput):
    rec = AGENTS.get(agent_id)
    if not rec:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' not found")
    if rec.get("archived_at"):
        raise HTTPException(status_code=409, detail=f"agent '{agent_id}' is archived")
    # A None field means "unchanged" (managed-agents convention) — top-level fields carry over.
    if params.name is not None:
        rec["name"] = params.name
    if params.model is not None:
        rec["model"] = params.model
    if params.system is not None:
        rec["system"] = params.system
    if params.allowed_tools is not None:
        rec["allowed_tools"] = params.allowed_tools
    new_version = int(rec.get("version", 1)) + 1
    now = time.time()
    rec["version"] = new_version
    rec["updated_at"] = now
    rec.setdefault("versions", {})[str(new_version)] = {
        "version": new_version, "name": rec["name"], "model": rec["model"],
        "system": rec["system"], "allowed_tools": rec["allowed_tools"], "created_at": now}
    AGENTS.put(agent_id, rec)
    return rec


@api.get("/v1/agents/{agent_id}/versions", tags=["Agents"], summary="List an agent's versions")
async def api_agent_versions(agent_id: str):
    rec = AGENTS.get(agent_id)
    if not rec:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' not found")
    versions = rec.get("versions") or {}
    data = [versions[k] for k in sorted(versions, key=lambda x: int(x))]
    return {"data": data}


@api.post("/v1/agents/{agent_id}/archive", tags=["Agents"], summary="Archive an agent")
async def api_agent_archive(agent_id: str):
    rec = AGENTS.get(agent_id)
    if not rec:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' not found")
    rec["archived_at"] = time.time()
    rec["updated_at"] = time.time()
    AGENTS.put(agent_id, rec)
    return rec


@api.post("/v1/sessions", status_code=201, tags=["Agent sessions"], summary="Create an agent session (boots a VM running the agent)")
async def api_session_create(params: SessionCreateInput):
    resources = [r.model_dump() for r in params.resources] if params.resources else None
    egress_policy = params.egress_policy.model_dump() if params.egress_policy else None
    try:
        return await _create_agent_session(params.agent_id, params.title,
                                           params.environment_id, params.agent_version,
                                           resources, params.agent, params.environment,
                                           egress_policy)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"session create failed: {e}")


@api.get("/v1/sessions", tags=["Agent sessions"], summary="List agent sessions")
async def api_session_list():
    return {"data": AGENT_SESSIONS.all()}


@api.get("/v1/sessions/{sid}", tags=["Agent sessions"], summary="Get an agent session")
async def api_session_get(sid: str):
    s = AGENT_SESSIONS.get(sid)
    if not s:
        raise HTTPException(status_code=404, detail=f"session '{sid}' not found")
    return s


@api.post("/v1/sessions/{sid}/events", tags=["Agent sessions"], summary="Send a user message (drive a turn)")
async def api_session_event(sid: str, params: SessionMessageInput):
    try:
        rec = await _session_send(sid, params.content)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"session_id": sid, "status": rec["status"]}


@api.get("/v1/sessions/{sid}/events/stream", tags=["Agent sessions"], summary="Stream session events (SSE)")
async def api_session_stream(sid: str, from_offset: int = 0):
    if not AGENT_SESSIONS.get(sid):
        raise HTTPException(status_code=404, detail=f"session '{sid}' not found")

    async def gen():
        async for ev in _stream_session_events(sid, from_off=from_offset):
            yield f"data: {json.dumps(ev)}\n\n"
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


async def _replay_events(sid: str) -> List[Dict]:
    """Drain all currently-available runner events (from offset 0) and return them parsed."""
    rec = AGENT_SESSIONS.get(sid)
    if not rec:
        return []
    vm_id = rec["vm_id"]
    out_off, buf, events = 0, "", []
    for _ in range(60):
        try:
            d = await _agent_runner_events(vm_id, sid, out_off)
        except Exception:
            break
        new = d.get("stdout", "")
        out_off = d.get("offset", out_off)
        buf += new
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            if line.strip():
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
        if d.get("done") or not new:
            break
    return events


@api.post("/v1/environments", status_code=201, tags=["Environments"], summary="Create an environment (VM spec)")
async def api_env_create(params: EnvironmentCreateInput):
    eid = "env_" + secrets.token_hex(12)
    rec = {"id": eid, "type": "environment", "name": params.name, "vcpu": params.vcpu,
           "mem_mb": params.mem_mb, "disk_mb": params.disk_mb, "created_at": time.time()}
    ENVIRONMENTS.put(eid, rec)
    return rec


@api.get("/v1/environments", tags=["Environments"], summary="List environments")
async def api_env_list():
    return {"data": ENVIRONMENTS.all()}


@api.get("/v1/environments/{eid}", tags=["Environments"], summary="Get an environment")
async def api_env_get(eid: str):
    e = ENVIRONMENTS.get(eid)
    if not e:
        raise HTTPException(status_code=404, detail=f"environment '{eid}' not found")
    return e


@api.delete("/v1/environments/{eid}", tags=["Environments"], summary="Delete an environment")
async def api_env_delete(eid: str):
    if not ENVIRONMENTS.get(eid):
        raise HTTPException(status_code=404, detail=f"environment '{eid}' not found")
    ENVIRONMENTS.delete(eid)
    return {"id": eid, "deleted": True}


@api.post("/v1/sessions/{sid}", tags=["Agent sessions"], summary="Update a session (title)")
async def api_session_update(sid: str, params: SessionUpdateInput):
    rec = AGENT_SESSIONS.get(sid)
    if not rec:
        raise HTTPException(status_code=404, detail=f"session '{sid}' not found")
    if params.title is not None:
        rec["title"] = params.title
    rec["updated_at"] = time.time()
    AGENT_SESSIONS.put(sid, rec)
    return rec


@api.delete("/v1/sessions/{sid}", tags=["Agent sessions"], summary="Terminate a session (destroy its VM)")
async def api_session_delete(sid: str):
    rec = AGENT_SESSIONS.get(sid)
    if not rec:
        raise HTTPException(status_code=404, detail=f"session '{sid}' not found")
    try:
        await api_vm_destroy(rec["vm_id"])
    except Exception as e:
        log.warning(f"session {sid} VM destroy failed: {e}")
    rec["status"] = "terminated"
    rec["updated_at"] = time.time()
    AGENT_SESSIONS.put(sid, rec)
    return {"session_id": sid, "status": "terminated"}


@api.post("/v1/sessions/{sid}/egress-policy", tags=["Agent sessions"], summary="Set a session's egress policy")
async def api_session_set_egress(sid: str, params: EgressPolicyInput):
    rec = AGENT_SESSIONS.get(sid)
    if not rec:
        raise HTTPException(status_code=404, detail=f"session '{sid}' not found")
    rec["egress_policy"] = _derive_egress_policy(sid, rec.get("resources"), params.model_dump())
    rec["updated_at"] = time.time()
    AGENT_SESSIONS.put(sid, rec)
    _rebuild_egress_index()
    return {"session_id": sid, "egress_policy": rec["egress_policy"]}


@api.get("/v1/sessions/{sid}/events", tags=["Agent sessions"], summary="Replay session events")
async def api_session_events(sid: str):
    if not AGENT_SESSIONS.get(sid):
        raise HTTPException(status_code=404, detail=f"session '{sid}' not found")
    return {"data": await _replay_events(sid)}


@api.get("/v1/sessions/{sid}/usage", tags=["Agent sessions"], summary="Get accumulated token usage + cost")
async def api_session_usage(sid: str):
    try:
        return {"session_id": sid, "usage": await _refresh_session_usage(sid)}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@mcp.tool(
    name="agent_run",
    annotations={"title": "Run a Claude agent in a microVM", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
async def agent_run(params: AgentRunInput, ctx: Context) -> str:
    """Boot a microVM running the Claude Agent SDK from the given agent definition, send one
    prompt, and return the agent's final result. JSON: {session_id, vm_id, result, events, usage}.
    For multi-turn use, prefer agent_session_create + agent_send_message."""
    try:
        rec = await _create_agent_session(params.agent_id, title="agent_run")
    except (KeyError, ValueError) as e:
        return json.dumps({"error": str(e)})
    sid = rec["id"]
    turn = await _drive_turn(sid, params.prompt, timeout=params.timeout)
    usage = None
    try:
        usage = await _refresh_session_usage(sid)
    except Exception:
        pass
    return json.dumps({"session_id": sid, "vm_id": rec["vm_id"], "result": turn["result"],
                       "events": turn["events"], "usage": usage}, indent=2)


class AgentSessionCreateToolInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., min_length=1)
    agent_version: Optional[int] = Field(default=None, ge=1)
    environment_id: Optional[str] = None
    title: Optional[str] = Field(default=None, max_length=200)


class AgentSendMessageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1, max_length=100000)
    timeout: int = Field(default=300, ge=1, le=3600)


@mcp.tool(
    name="agent_session_create",
    annotations={"title": "Create a persistent Claude agent session (microVM)", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
async def agent_session_create(params: AgentSessionCreateToolInput, ctx: Context) -> str:
    """Boot a microVM running the Claude Agent SDK from an agent definition and keep it warm for
    multi-turn use. Returns JSON {session_id, vm_id, status}. Drive turns with agent_send_message."""
    try:
        rec = await _create_agent_session(params.agent_id, params.title,
                                          params.environment_id, params.agent_version)
    except (KeyError, ValueError) as e:
        return json.dumps({"error": str(e)})
    return json.dumps({"session_id": rec["id"], "vm_id": rec["vm_id"], "status": rec["status"]}, indent=2)


@mcp.tool(
    name="agent_send_message",
    annotations={"title": "Send a message to a Claude agent session", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
async def agent_send_message(params: AgentSendMessageInput, ctx: Context) -> str:
    """Send a user message to an existing agent session (conversation state is retained across
    turns) and return that turn's final result. JSON {session_id, result, events, usage}."""
    try:
        turn = await _drive_turn(params.session_id, params.content, timeout=params.timeout)
    except KeyError as e:
        return json.dumps({"error": str(e)})
    usage = None
    try:
        usage = await _refresh_session_usage(params.session_id)
    except Exception:
        pass
    return json.dumps({"session_id": params.session_id, "result": turn["result"],
                       "events": turn["events"], "usage": usage}, indent=2)


# ─── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Firecracker VM server")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    uvicorn.run(MCPRouter(api, _mcp_handler), host=args.host, port=args.port)
