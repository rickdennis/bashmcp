from __future__ import annotations

import json
import shlex

import httpx
import pytest

from broker import app
from tests.conftest import client_error, events, make_ctx, make_jwt

TOKEN = make_jwt(sub="user-1", **{"cognito:username": "rick"}, email="rick@example.com")


async def test_bash_exec_creates_sandbox_and_runs(services, fake_client):
    fake_client.invoke_results = [events("hello\n", "", 0), events("again\n", "", 0)]
    first = json.loads(await app.bash_exec("echo hello", ctx=make_ctx(TOKEN)))
    assert first["stdout"] == "hello\n"
    assert first["returncode"] == 0
    assert first["status"] == "COMPLETED"
    assert first["workspace"] == "default"
    assert first["cold_start"] is True
    assert len(first["runtime_session_id"]) >= 33
    sent = fake_client.invoke_calls[0]["body"]
    assert sent["timeout"] == 60
    assert "cd /mnt/workspace || exit 1" in sent["command"]
    assert sent["command"].endswith("echo hello'")
    assert "export HOME=/mnt/workspace/.home" in sent["command"]

    second = json.loads(await app.bash_exec("echo again", ctx=make_ctx(TOKEN), working_dir="/mnt/workspace/a b", timeout=5))
    assert second["runtime_session_id"] == first["runtime_session_id"]
    assert second["cold_start"] is False
    inner = shlex.split(fake_client.invoke_calls[1]["body"]["command"])[2]
    assert "cd '/mnt/workspace/a b' || exit 1" in inner
    assert fake_client.invoke_calls[1]["body"]["timeout"] == 5


async def test_bash_exec_isolated_per_user(services, fake_client):
    a = json.loads(await app.bash_exec("id", ctx=make_ctx(make_jwt(sub="user-a"))))
    b = json.loads(await app.bash_exec("id", ctx=make_ctx(make_jwt(sub="user-b"))))
    assert a["runtime_session_id"] != b["runtime_session_id"]


async def test_bash_exec_rejects_missing_auth(services):
    out = json.loads(await app.bash_exec("id", ctx=make_ctx()))
    assert out["error"].startswith("Unauthorized")


async def test_bash_exec_rejects_wrong_issuer(services):
    out = json.loads(await app.bash_exec("id", ctx=make_ctx(make_jwt(sub="x", iss="https://evil.example"))))
    assert "issuer" in out["error"]


@pytest.mark.parametrize("timeout", [0, -1, 601, 100000])
async def test_bash_exec_timeout_bounds(services, timeout):
    out = json.loads(await app.bash_exec("id", ctx=make_ctx(TOKEN), timeout=timeout))
    assert "timeout must be between 1 and 600" in out["error"]


async def test_bash_exec_empty_command(services):
    out = json.loads(await app.bash_exec("   ", ctx=make_ctx(TOKEN)))
    assert "empty" in out["error"]


async def test_bash_exec_invalid_workspace(services):
    out = json.loads(await app.bash_exec("id", ctx=make_ctx(TOKEN), workspace="no spaces"))
    assert "workspace" in out["error"]


async def test_bash_exec_timed_out_marks_returncode(services, fake_client):
    fake_client.invoke_results = [events("partial", "warn", None, "TIMED_OUT")]
    out = json.loads(await app.bash_exec("sleep 99", ctx=make_ctx(TOKEN), timeout=1))
    assert out["returncode"] == -1
    assert out["status"] == "TIMED_OUT"
    assert out["stdout"] == "partial"
    assert out["stderr"] == "warn\nCommand timed out"


async def test_bash_exec_reports_sandbox_error(services, fake_client):
    fake_client.invoke_results = [client_error("AccessDeniedException", "nope", 403)]
    out = json.loads(await app.bash_exec("id", ctx=make_ctx(TOKEN)))
    assert "AccessDeniedException" in out["error"]
    assert out["workspace"] == "default"
    assert len(out["runtime_session_id"]) >= 33


async def test_sandbox_lifecycle(services, fake_client):
    ctx = make_ctx(TOKEN)
    created = json.loads(await app.sandbox_new(ctx, "proj", label="project box"))
    assert created["created"] is True and created["label"] == "project box"

    dup = json.loads(await app.sandbox_new(ctx, "proj"))
    assert "already exists" in dup["error"]

    listing = json.loads(await app.sandbox_list(ctx))
    assert listing["count"] == 1 and listing["sandboxes"][0]["workspace"] == "proj"
    assert "user_sub" not in listing["sandboxes"][0]

    status = json.loads(await app.sandbox_status(ctx, "proj"))
    assert status["likely_stopped"] is False and status["idle_seconds"] >= 0

    paused = json.loads(await app.sandbox_pause(ctx, "proj"))
    assert paused["status"] == "paused" and paused["stop_result"] == "stopped"
    assert fake_client.stop_calls[0]["runtimeSessionId"] == created["runtime_session_id"]

    status = json.loads(await app.sandbox_status(ctx, "proj"))
    assert status["likely_stopped"] is True and status["status"] == "paused"

    fake_client.invoke_results = [events("resumed\n")]
    resumed = json.loads(await app.bash_exec("echo resumed", ctx, workspace="proj"))
    assert resumed["cold_start"] is True and resumed["stdout"] == "resumed\n"
    assert json.loads(await app.sandbox_status(ctx, "proj"))["status"] == "active"

    destroyed = json.loads(await app.sandbox_destroy(ctx, "proj"))
    assert destroyed["status"] == "destroyed"
    assert json.loads(await app.sandbox_list(ctx))["count"] == 0
    assert "No sandbox named 'proj'" in json.loads(await app.sandbox_status(ctx, "proj"))["error"]


async def test_management_tools_require_auth(services):
    for coro in (app.sandbox_list(make_ctx()), app.sandbox_status(make_ctx()), app.sandbox_pause(make_ctx()),
                 app.sandbox_new(make_ctx(), "x"), app.sandbox_destroy(make_ctx())):
        assert json.loads(await coro)["error"].startswith("Unauthorized")


async def test_http_layer_forwards_authorization_header(services, fake_client):
    """Drive the real streamable-HTTP ASGI app once: proves the header plumbing FastMCP -> tools."""
    asgi = app.mcp.streamable_http_app()
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN}",
    }
    fake_client.invoke_results = [events("via-http\n")]
    async with app.mcp.session_manager.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi), base_url="http://testserver") as client:
            health = await client.get("/healthz")
            assert health.status_code == 200 and health.json()["status"] == "ok"

            listed = await client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            assert listed.status_code == 200, listed.text
            names = sorted(t["name"] for t in listed.json()["result"]["tools"])
            assert names == ["bash_exec", "sandbox_destroy", "sandbox_list", "sandbox_new", "sandbox_pause", "sandbox_status"]

            called = await client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "bash_exec", "arguments": {"command": "echo via-http"}}})
            assert called.status_code == 200, called.text
            text = called.json()["result"]["content"][0]["text"]
            assert json.loads(text)["stdout"] == "via-http\n"

            denied = await client.post("/mcp", headers={k: v for k, v in headers.items() if k != "Authorization"}, json={
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "sandbox_list", "arguments": {}}})
            assert denied.status_code == 200
            assert json.loads(denied.json()["result"]["content"][0]["text"])["error"].startswith("Unauthorized")
