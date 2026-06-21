"""Egress-broker helpers: repo-slug parsing, per-session policy derivation, and the
ip->policy index the node-agent writes for fc-egress. Dependency-free:

    FC_BASE_DIR=$(mktemp -d) uv run python tests/test_egress.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["FC_BASE_DIR"] = tempfile.mkdtemp(prefix="fcmcp-egress-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


def test_repo_slug():
    f = server._repo_slug
    assert f("acme/widgets") == "acme/widgets"
    assert f("https://github.com/acme/widgets") == "acme/widgets"
    assert f("https://github.com/acme/widgets.git") == "acme/widgets"
    assert f("git@github.com:acme/widgets.git") == "acme/widgets"
    assert f("https://github.com/acme/widgets/") == "acme/widgets"
    print("PASS: _repo_slug parses shorthand / https / ssh / .git / trailing slash")


def test_derive_egress_policy_from_resources():
    resources = [
        {"type": "github_repository", "repository_url": "acme/widgets"},
        {"type": "github_repository", "repository_url": "https://github.com/acme/other.git"},
        {"type": "file", "path": "/x"},  # not a github resource -> ignored
    ]
    pol = server._derive_egress_policy("sesn_1", resources, None)
    assert pol["session_id"] == "sesn_1"
    assert pol["mode"] == "default_deny"
    assert set(pol["github"]["repos"]) == {"acme/widgets", "acme/other"}
    assert "github.com" in pol["allowed_hosts"]
    assert "*.githubusercontent.com" in pol["allowed_hosts"]
    print("PASS: _derive_egress_policy derives repos + hosts from github resources")


def test_derive_egress_policy_explicit_merge():
    resources = [{"type": "github_repository", "repository_url": "acme/widgets"}]
    explicit = {"allowed_hosts": ["pypi.org"], "github": {"repos": ["acme/extra"]}}
    pol = server._derive_egress_policy("sesn_1", resources, explicit)
    assert "pypi.org" in pol["allowed_hosts"]          # explicit added
    assert "github.com" in pol["allowed_hosts"]        # derived retained
    assert set(pol["github"]["repos"]) == {"acme/widgets", "acme/extra"}
    print("PASS: _derive_egress_policy merges explicit over derived")


def test_derive_egress_policy_none_when_empty():
    pol = server._derive_egress_policy("sesn_1", [{"type": "file", "path": "/x"}], None)
    assert pol is None  # nothing to allow -> default-deny everywhere (no index entry)
    print("PASS: _derive_egress_policy returns None when there is nothing to permit")


def test_build_egress_index():
    vm_records = [
        {"vm_id": "vm-a", "slot": 7, "ip_address": "172.16.0.7"},
        {"vm_id": "vm-b", "slot": 8, "ip_address": "172.16.0.8"},
        {"vm_id": "vm-c", "slot": 9, "ip_address": "172.16.0.9"},  # no session references it
    ]
    sessions = [
        {"id": "sesn_1", "vm_id": "vm-a",
         "egress_policy": {"session_id": "sesn_1", "github": {"repos": ["acme/widgets"]}}},
        {"id": "sesn_2", "vm_id": "vm-b", "egress_policy": None},   # no policy -> excluded
        {"id": "sesn_3", "vm_id": "vm-missing"},                    # vm absent -> skipped
    ]
    idx = server._build_egress_index(vm_records, sessions)
    assert idx["172.16.0.7"]["github"]["repos"] == ["acme/widgets"]
    assert "172.16.0.8" not in idx  # session present but no policy
    assert "172.16.0.9" not in idx  # vm present but no session
    print("PASS: _build_egress_index joins vm-ip to session policy")


def test_rebuild_egress_index_writes_file():
    server._vm_state.create("vm-x", {"vm_id": "vm-x", "slot": 5, "ip_address": "172.16.0.5",
                                     "status": "running"})
    server.AGENT_SESSIONS.put("sesn_x", {"id": "sesn_x", "vm_id": "vm-x",
                                         "egress_policy": {"session_id": "sesn_x",
                                                           "github": {"repos": ["acme/widgets"]}}})
    server._rebuild_egress_index()
    data = json.loads(server.EGRESS_INDEX_PATH.read_text())
    assert data["172.16.0.5"]["github"]["repos"] == ["acme/widgets"]
    print("PASS: _rebuild_egress_index writes the ip->policy index atomically")


def test_session_create_input_accepts_egress_policy():
    inp = server.SessionCreateInput(
        agent_id="a",
        egress_policy={"allowed_hosts": ["pypi.org"], "github": {"repos": ["acme/widgets"]}},
    )
    assert inp.egress_policy is not None
    derived = server._derive_egress_policy("sesn_1", None, inp.egress_policy.model_dump())
    assert "pypi.org" in derived["allowed_hosts"]
    assert "acme/widgets" in derived["github"]["repos"]
    print("PASS: SessionCreateInput accepts and round-trips an egress_policy")


def test_egress_policy_input_rejects_unknown_field():
    try:
        server.EgressPolicyInput(bogus=1)
    except Exception:
        print("PASS: EgressPolicyInput rejects unknown fields (extra=forbid)")
        return
    raise AssertionError("EgressPolicyInput should reject unknown fields")


if __name__ == "__main__":
    test_repo_slug()
    test_derive_egress_policy_from_resources()
    test_derive_egress_policy_explicit_merge()
    test_derive_egress_policy_none_when_empty()
    test_build_egress_index()
    test_rebuild_egress_index_writes_file()
    test_session_create_input_accepts_egress_policy()
    test_egress_policy_input_rejects_unknown_field()
    print("\nAll egress checks passed.")
