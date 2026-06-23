"""Logic-level checks for POST /vms/{vm_id}/exec (the fcctl admin exec endpoint).

Dependency-free; run with `FC_BASE_DIR=$(mktemp -d) uv run python tests/test_vm_exec.py`.
The full SSH path needs Linux+KVM, so these only assert the guard rails the endpoint
must enforce before it ever shells out: 404 for a missing VM, 409 for a non-running one.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["FC_BASE_DIR"] = tempfile.mkdtemp(prefix="fcmcp-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from fastapi import HTTPException  # noqa: E402


def _call(vm_id, command="echo hi"):
    params = server.VmExecInput(command=command)
    return asyncio.run(server.api_vm_exec(vm_id, params))


def test_exec_missing_vm_404():
    server._vm_state._vms.clear() if hasattr(server._vm_state, "_vms") else None
    try:
        _call("does-not-exist")
    except HTTPException as e:
        assert e.status_code == 404, e.status_code
        print("PASS: exec on a missing VM returns 404")
        return
    raise AssertionError("expected HTTPException(404) for a missing VM")


def test_exec_non_running_vm_409():
    # A paused VM is auto-resumed by the endpoint (see commit 601d143), so to exercise the
    # "not running" guard we use a VM that is neither running nor paused (e.g. errored).
    vm_id = "vm-errored-exec"
    server._vm_state.create(vm_id, {
        "vm_id": vm_id, "name": "vm-errored-exec", "status": "error",
    })
    try:
        _call(vm_id)
    except HTTPException as e:
        assert e.status_code == 409, e.status_code
        assert "not running" in str(e.detail), e.detail
        print("PASS: exec on a non-running VM returns 409 (with status in detail)")
        return
    finally:
        server._vm_state.delete(vm_id)
    raise AssertionError("expected HTTPException(409) for a non-running VM")


def _run():
    test_exec_missing_vm_404()
    test_exec_non_running_vm_409()
    print("\nAll vm-exec checks passed.")


if __name__ == "__main__":
    _run()
