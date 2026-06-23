"""Phase-0 correctness checks: stable slot allocation + startup reconcile.

Dependency-free; run with `FC_BASE_DIR=$(mktemp -d) uv run python tests/test_phase0.py`.
These cover the node-local invariants that the HA control plane later relies on:
slots never drift across destroy/recreate, and a process restart re-classifies VMs.
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

# Isolate state under a throwaway dir BEFORE importing the server module.
os.environ["FC_BASE_DIR"] = tempfile.mkdtemp(prefix="fcmcp-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


def test_slot_stability_across_destroy_recreate():
    """The original bug: destroying an early VM shifted later VMs' computed tap/IP.
    With explicit stored slots, a survivor's slot must never move."""
    server._slots.rebuild_from_records([])
    a, b, c = server._slots.allocate(), server._slots.allocate(), server._slots.allocate()
    assert (a, b, c) == (2, 3, 4), (a, b, c)

    # Destroy the *middle* VM (slot 3). Survivors a=2 and c=4 must keep their slots.
    server._slots.release(b)
    # A new VM reuses the freed low slot (3), never bumping c off 4.
    d = server._slots.allocate()
    assert d == 3, d
    e = server._slots.allocate()
    assert e == 5, e  # next fresh slot, c==4 untouched
    print("PASS: slot allocation is stable across destroy/recreate (no positional drift)")


def test_slot_capacity_cap():
    cap = server.SLOT_MAX - server.SLOT_MIN + 1
    server._slots.rebuild_from_records([])
    got = [server._slots.allocate() for _ in range(cap)]
    assert None not in got and len(set(got)) == cap, got
    assert server._slots.allocate() is None  # cap+1 allocation refused
    assert server._slots.free_count() == 0
    print(f"PASS: allocator caps at {cap} slots and refuses the next one")


def test_legacy_record_slot_derivation():
    """Records persisted before the slot field must still resolve via ip_address."""
    assert server._record_slot({"ip_address": "172.16.0.7"}) == 7
    assert server._record_slot({"slot": 9, "ip_address": "172.16.0.7"}) == 9  # explicit wins
    assert server._record_slot({}) is None
    print("PASS: legacy records derive slot from ip_address")


def test_reconcile_classifies_and_rebuilds_freelist(monkeyish):
    """running+live -> running(+slot reserved); dead+snapshot -> paused; dead+none -> error."""
    base = Path(os.environ["FC_BASE_DIR"])
    (base / "sockets").mkdir(parents=True, exist_ok=True)
    (base / "snapshots" / "vm-paused").mkdir(parents=True, exist_ok=True)
    mem = base / "snapshots" / "vm-paused" / "memory.bin"; mem.write_bytes(b"x")
    st = base / "snapshots" / "vm-paused" / "vmstate.bin"; st.write_bytes(b"x")
    live_sock = base / "sockets" / "vm-live.sock"; live_sock.write_text("")

    server._vm_state._vms = {
        "vm-live":   {"vm_id": "vm-live",   "status": "running", "pid": 4242, "slot": 2, "ip_address": "172.16.0.2", "snapshot": None},
        "vm-paused": {"vm_id": "vm-paused", "status": "running", "pid": 9999, "slot": 3, "ip_address": "172.16.0.3",
                       "snapshot": {"mem_path": str(mem), "state_path": str(st)}},
        "vm-dead":   {"vm_id": "vm-dead",   "status": "running", "pid": 9998, "slot": 4, "ip_address": "172.16.0.4", "snapshot": None},
    }

    # Deterministic, cross-platform liveness: only vm-live's pid (4242) is "firecracker".
    monkeyish("_pid_is_firecracker", lambda pid: pid == 4242)

    asyncio.run(server.reconcile_on_startup())

    assert server._vm_state.get("vm-live")["status"] == "running"
    assert server._vm_state.get("vm-paused")["status"] == "paused"
    assert server._vm_state.get("vm-dead")["status"] == "error"
    # All three survivors keep their slots reserved -> 3 used, cap-3 free.
    cap = server.SLOT_MAX - server.SLOT_MIN + 1
    assert server._slots.free_count() == cap - 3, server._slots.free_count()
    assert server._ready is True
    print("PASS: reconcile classifies running/paused/error and rebuilds the free-list")


def _run():
    # tiny monkeypatch helper so we avoid a pytest dependency
    originals = {}
    def monkeyish(attr, value):
        originals[attr] = getattr(server, attr)
        setattr(server, attr, value)
    try:
        test_slot_stability_across_destroy_recreate()
        test_slot_capacity_cap()
        test_legacy_record_slot_derivation()
        test_reconcile_classifies_and_rebuilds_freelist(monkeyish)
    finally:
        for k, v in originals.items():
            setattr(server, k, v)
    print("\nAll Phase-0 checks passed.")


if __name__ == "__main__":
    _run()
