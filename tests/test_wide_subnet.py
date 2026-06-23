"""Wide-subnet tests: /16 IP formula, legacy slot derivation, tap name, SLOT_MAX from env.

    FC_BASE_DIR=$(mktemp -d) uv run python tests/test_wide_subnet.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["FC_BASE_DIR"] = tempfile.mkdtemp(prefix="fcmcp-wide-")
os.environ.setdefault("FC_MAX_VMS", "500")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


def test_slot_to_ip_low():
    """Slots 2–255 stay in the .0.x range (backwards-compatible with /24 records)."""
    assert server._slot_to_ip(2)   == "172.16.0.2"
    assert server._slot_to_ip(33)  == "172.16.0.33"
    assert server._slot_to_ip(255) == "172.16.0.255"
    print("PASS: _slot_to_ip: low slots stay in 172.16.0.x")


def test_slot_to_ip_high():
    """Slots >= 256 roll over to the next /16 octet."""
    assert server._slot_to_ip(256) == "172.16.1.0"
    assert server._slot_to_ip(257) == "172.16.1.1"
    assert server._slot_to_ip(511) == "172.16.1.255"
    assert server._slot_to_ip(512) == "172.16.2.0"
    print("PASS: _slot_to_ip: high slots use 172.16.Y.X")


def test_ip_to_slot_roundtrip():
    """_ip_to_slot is the inverse of _slot_to_ip for valid slots."""
    for slot in [2, 33, 255, 256, 500, 1000, 65533]:
        ip = server._slot_to_ip(slot)
        assert server._ip_to_slot(ip) == slot, f"roundtrip failed for slot {slot} → {ip}"
    print("PASS: _ip_to_slot / _slot_to_ip roundtrip")


def test_legacy_record_slot_derivation():
    """Records written under the old /24 scheme (ip_address=172.16.0.X) still parse."""
    assert server._record_slot({"ip_address": "172.16.0.7"})  == 7
    assert server._record_slot({"ip_address": "172.16.0.33"}) == 33
    assert server._record_slot({"slot": 9, "ip_address": "172.16.0.7"}) == 9  # explicit wins
    assert server._record_slot({"ip_address": "172.16.1.0"})  == 256  # new range
    assert server._record_slot({}) is None
    print("PASS: legacy _record_slot handles both /24 and /16 ip_address values")


def test_vm_tap_name():
    """Tap name is a hex-encoded 0-based offset from SLOT_MIN (not from slot number)."""
    assert server._slot_to_tap(2)   == "fc-tap-00000000"  # slot 2 = index 0
    assert server._slot_to_tap(3)   == "fc-tap-00000001"
    assert server._slot_to_tap(257) == "fc-tap-000000ff"  # slot 257 = index 255
    assert server._slot_to_tap(258) == "fc-tap-00000100"  # slot 258 = index 256
    print("PASS: _slot_to_tap produces correct hex names")


def test_slot_max_from_env():
    """SLOT_MAX is driven by FC_MAX_VMS; default 500."""
    assert server.SLOT_MAX == server.SLOT_MIN + int(os.environ["FC_MAX_VMS"]) - 1
    print(f"PASS: SLOT_MAX={server.SLOT_MAX} from FC_MAX_VMS={os.environ['FC_MAX_VMS']}")


def test_slot_max_default():
    """Default FC_MAX_VMS when env is not set."""
    import importlib, server as s
    old = os.environ.pop("FC_MAX_VMS", None)
    try:
        importlib.reload(s)
        # default is 500
        assert s.SLOT_MAX == s.SLOT_MIN + 500 - 1, f"default SLOT_MAX={s.SLOT_MAX}"
        print(f"PASS: default SLOT_MAX={s.SLOT_MAX} (FC_MAX_VMS=500)")
    finally:
        if old is not None:
            os.environ["FC_MAX_VMS"] = old
        importlib.reload(s)  # restore


if __name__ == "__main__":
    test_slot_to_ip_low()
    test_slot_to_ip_high()
    test_ip_to_slot_roundtrip()
    test_legacy_record_slot_derivation()
    test_vm_tap_name()
    test_slot_max_from_env()
    test_slot_max_default()
    print("\nAll wide-subnet checks passed.")
