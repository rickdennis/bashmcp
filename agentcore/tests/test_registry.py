from __future__ import annotations

import pytest

from broker.auth import Identity
from broker.registry import InvalidWorkspace, SandboxExists, SandboxRegistry, new_session_id

RICK = Identity(sub="user-1", username="rick", email="rick@example.com")
OTHER = Identity(sub="user-2")


def test_new_session_id_meets_agentcore_minimum():
    sid = new_session_id()
    assert len(sid) >= 33
    assert all(c.isalnum() or c in "-_" for c in sid)


def test_create_and_get(ddb_table):
    reg = SandboxRegistry(ddb_table)
    created = reg.create(RICK, "default", label="my box")
    got = reg.get("user-1", "default")
    assert got is not None
    assert got.runtime_session_id == created.runtime_session_id
    assert got.label == "my box"
    assert got.user_email == "rick@example.com"
    assert got.status == "new"
    item = ddb_table.get_item(Key={"user_sub": "user-1", "workspace": "default"})["Item"]
    assert int(item["expires_at"]) > 0


def test_create_twice_conflicts(ddb_table):
    reg = SandboxRegistry(ddb_table)
    reg.create(RICK, "default")
    with pytest.raises(SandboxExists):
        reg.create(RICK, "default")


def test_get_or_create_is_idempotent(ddb_table):
    reg = SandboxRegistry(ddb_table)
    first, created = reg.get_or_create(RICK, "default")
    second, created_again = reg.get_or_create(RICK, "default")
    assert created is True and created_again is False
    assert first.runtime_session_id == second.runtime_session_id


def test_list_is_scoped_per_user(ddb_table):
    reg = SandboxRegistry(ddb_table)
    reg.create(RICK, "zeta")
    reg.create(RICK, "alpha")
    reg.create(OTHER, "default")
    names = [r.workspace for r in reg.list_for_user("user-1")]
    assert names == ["alpha", "zeta"]
    assert [r.workspace for r in reg.list_for_user("user-2")] == ["default"]
    assert reg.list_for_user("nobody") == []


def test_touch_and_set_status(ddb_table):
    reg = SandboxRegistry(ddb_table)
    row = reg.create(RICK, "default")
    reg.touch("user-1", "default", "active")
    touched = reg.get("user-1", "default")
    assert touched.status == "active"
    assert touched.last_used_at >= row.last_used_at
    reg.set_status("user-1", "default", "paused")
    assert reg.get("user-1", "default").status == "paused"
    reg.touch("user-1", "missing")  # no row: silently ignored
    assert reg.get("user-1", "missing") is None


def test_delete(ddb_table):
    reg = SandboxRegistry(ddb_table)
    reg.create(RICK, "default")
    assert reg.delete("user-1", "default") is True
    assert reg.get("user-1", "default") is None
    assert reg.delete("user-1", "default") is False


@pytest.mark.parametrize("bad", ["", "bad name", "../x", "-leading", "x" * 65, "sp/ace"])
def test_invalid_workspace_names(ddb_table, bad):
    reg = SandboxRegistry(ddb_table)
    with pytest.raises(InvalidWorkspace):
        reg.create(RICK, bad)


def test_public_view_hides_sub(ddb_table):
    reg = SandboxRegistry(ddb_table)
    row = reg.create(RICK, "default")
    public = row.to_public()
    assert "user_sub" not in public
    assert public["workspace"] == "default"
    assert row.idle_seconds() >= 0
