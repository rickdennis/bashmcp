"""Operator UI (ui/app.py) against injected fakes: moto DynamoDB, FakeAgentCoreClient, and small
fakes for the control-plane and CloudWatch Logs clients."""
from __future__ import annotations

import json
import shlex
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from broker.executor import SandboxExecutor, build_script
from tests.conftest import SANDBOX_ARN, FakeAgentCoreClient, client_error, events
from ui import app as ui_app

RUNTIME_ID = SANDBOX_ARN.rsplit("/", 1)[-1]
UI_HEADERS = {ui_app.UI_HEADER: "1"}

SID_RICK_DEFAULT = "11111111-1111-4111-8111-111111111111"
SID_RICK_PROJ = "22222222-2222-4222-8222-222222222222"
SID_OTHER = "33333333-3333-4333-8333-333333333333"


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class FakeControl:
    def __init__(self) -> None:
        self.get_calls: list[dict[str, Any]] = []

    def list_agent_runtimes(self, **kwargs: Any) -> dict[str, Any]:
        if not kwargs.get("nextToken"):  # first page: a decoy, then a continuation token
            return {"agentRuntimes": [{"agentRuntimeName": "other_runtime", "agentRuntimeArn": "arn:x/other-1"}], "nextToken": "p2"}
        return {"agentRuntimes": [{"agentRuntimeName": "bashmcp_sandbox_nonprod", "agentRuntimeArn": SANDBOX_ARN, "agentRuntimeId": RUNTIME_ID}]}

    def get_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self.get_calls.append(kwargs)
        return {
            "agentRuntimeId": kwargs["agentRuntimeId"],
            "status": "READY",
            "agentRuntimeVersion": "7",
            "lastUpdatedAt": datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
            "description": "sandbox",
        }


class FakeLogs:
    def __init__(self, pages: list[dict[str, Any]] | None = None) -> None:
        self.pages = list(pages or [])
        self.calls: list[dict[str, Any]] = []

    def filter_log_events(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if not self.pages:
            return {"events": []}
        return self.pages.pop(0)


@pytest.fixture
def seeded_table(ddb_table):
    now = datetime.now(timezone.utc)
    rows = [
        {  # active: used seconds ago
            "user_sub": "sub-rick", "workspace": "default", "runtime_session_id": SID_RICK_DEFAULT,
            "status": "active", "created_at": _iso(now - timedelta(days=2)), "last_used_at": _iso(now - timedelta(seconds=30)),
            "user_email": "rick.dennis@stoneridgeam.com", "user_name": "rick", "expires_at": int(now.timestamp()) + 86400,
        },
        {  # paused by the broker (recently used, but paused wins)
            "user_sub": "sub-rick", "workspace": "proj", "runtime_session_id": SID_RICK_PROJ,
            "status": "paused", "created_at": _iso(now - timedelta(days=1)), "last_used_at": _iso(now - timedelta(minutes=2)),
            "user_email": "rick.dennis@stoneridgeam.com", "label": "side project", "expires_at": int(now.timestamp()) + 86400,
        },
        {  # idle for two hours -> likely stopped by the runtime idle timeout
            "user_sub": "sub-other", "workspace": "default", "runtime_session_id": SID_OTHER,
            "status": "active", "created_at": _iso(now - timedelta(days=5)), "last_used_at": _iso(now - timedelta(hours=2)),
            "user_email": "other.person@stoneridgeam.com", "expires_at": int(now.timestamp()) + 86400,
        },
    ]
    for row in rows:
        ddb_table.put_item(Item=row)
    return ddb_table


@pytest.fixture
def fake_logs() -> FakeLogs:
    return FakeLogs()


@pytest.fixture
def fake_control() -> FakeControl:
    return FakeControl()


@pytest.fixture
def ui(seeded_table, fake_client: FakeAgentCoreClient, fake_control: FakeControl, fake_logs: FakeLogs):
    runtime = ui_app.resolve_runtime(fake_control, "bashmcp_sandbox_nonprod")
    svc = ui_app.Services(
        table=seeded_table,
        executor=SandboxExecutor(fake_client, runtime.arn, conflict_max_wait=5, sleep=lambda _s: None),
        control=fake_control,
        logs=fake_logs,
        runtime=runtime,
        region="us-east-1",
        profile="test-profile",
        table_name="bashmcp-sandboxes",
        idle_seconds=1800,
        caller_arn="arn:aws:sts::123456789012:assumed-role/ops/rick",
    )
    with TestClient(ui_app.create_app(svc), base_url="http://127.0.0.1:8787") as client:
        yield client


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for block in text.strip().split("\n\n"):
        name, data = "message", ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        out.append((name, json.loads(data) if data else {}))
    return out


# ------------------------------------------------------------------------------------------------


def test_resolve_runtime_paginates_and_derives_log_group(fake_control):
    rt = ui_app.resolve_runtime(fake_control, "bashmcp_sandbox_nonprod")
    assert rt.arn == SANDBOX_ARN
    assert rt.id == RUNTIME_ID
    assert rt.log_group == f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT"
    with pytest.raises(RuntimeError):
        ui_app.resolve_runtime(fake_control, "does_not_exist")


def test_index_serves_banner(ui):
    resp = ui.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert ui_app.BANNER in resp.text
    assert "<script src=" not in resp.text and "<link " not in resp.text  # inline JS/CSS only, no CDN assets


def test_runtime_endpoint(ui, fake_control):
    body = ui.get("/api/runtime").json()
    assert body["name"] == "bashmcp_sandbox_nonprod"
    assert body["id"] == RUNTIME_ID
    assert body["arn"] == SANDBOX_ARN
    assert body["status"] == "READY"
    assert body["version"] == "7"
    assert body["last_updated"].startswith("2026-09-10T12:00:00")
    assert body["region"] == "us-east-1"
    assert body["profile"] == "test-profile"
    assert body["log_group"].endswith(f"{RUNTIME_ID}-DEFAULT")
    assert fake_control.get_calls == [{"agentRuntimeId": RUNTIME_ID}]


def test_sandboxes_state_inference(ui):
    body = ui.get("/api/sandboxes").json()
    assert body["count"] == 3
    assert body["table"] == "bashmcp-sandboxes"
    by_key = {(r["user_email"], r["workspace"]): r for r in body["sandboxes"]}
    rick_default = by_key[("rick.dennis@stoneridgeam.com", "default")]
    rick_proj = by_key[("rick.dennis@stoneridgeam.com", "proj")]
    other = by_key[("other.person@stoneridgeam.com", "default")]
    assert rick_default["state"] == "active" and rick_default["idle_seconds"] < 120
    assert rick_proj["state"] == "paused" and rick_proj["label"] == "side project"
    assert other["state"] == "likely stopped" and other["idle_seconds"] >= 7200
    assert other["status"] == "active"  # raw registry status is still exposed
    assert rick_default["runtime_session_id"] == SID_RICK_DEFAULT
    assert isinstance(rick_default["expires_at"], int)  # Decimal converted
    # sorted by (email, workspace)
    assert [(r["user_email"], r["workspace"]) for r in body["sandboxes"]] == [
        ("other.person@stoneridgeam.com", "default"),
        ("rick.dennis@stoneridgeam.com", "default"),
        ("rick.dennis@stoneridgeam.com", "proj"),
    ]


def test_infer_state_rules():
    assert ui_app.infer_state("paused", 5, 1800) == "paused"
    assert ui_app.infer_state("active", 1800, 1800) == "active"
    assert ui_app.infer_state("active", 1801, 1800) == "likely stopped"
    assert ui_app.infer_state("new", None, 1800) == "new"


def test_exec_happy_path_streams_sse(ui, fake_client, seeded_table):
    fake_client.invoke_results = [events("hello\n", "warn\n", 0)]
    resp = ui.post("/api/exec", json={"runtime_session_id": SID_RICK_DEFAULT, "command": "echo hello"}, headers=UI_HEADERS)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    evs = parse_sse(resp.text)
    names = [n for n, _ in evs]
    assert names[0] == "start"
    assert names[-1] == "done"
    assert "stdout" in names and "stderr" in names
    data = dict(evs)
    assert data["start"]["runtime_session_id"] == SID_RICK_DEFAULT
    assert data["start"]["workspace"] == "default"
    assert data["start"]["working_dir"] == "/mnt/workspace"
    assert data["start"]["timeout"] == 60
    assert data["stdout"]["data"] == "hello\n"
    assert data["stderr"]["data"] == "warn\n"
    assert data["done"]["exit_code"] == 0
    assert data["done"]["status"] == "COMPLETED"
    assert data["done"]["attempts"] == 1
    assert isinstance(data["done"]["elapsed_seconds"], (int, float))
    # the call went to the right session with the wrapped script
    call = fake_client.invoke_calls[0]
    assert call["runtimeSessionId"] == SID_RICK_DEFAULT
    assert call["agentRuntimeArn"] == SANDBOX_ARN
    assert call["body"] == {"command": build_script("echo hello", "/mnt/workspace", "/mnt/workspace/.home"), "timeout": 60}
    # the registry row was touched (microVM is running again)
    item = seeded_table.get_item(Key={"user_sub": "sub-rick", "workspace": "default"})["Item"]
    assert item["status"] == "active"
    assert ui_app.idle_seconds_of(item) < 5


def test_exec_passes_working_dir_and_timeout(ui, fake_client):
    fake_client.invoke_results = [events("x", "", 0)]
    resp = ui.post("/api/exec", json={"runtime_session_id": SID_OTHER, "command": "pwd", "working_dir": "/mnt/workspace/a b", "timeout": 5},
                   headers=UI_HEADERS)
    assert resp.status_code == 200
    body = fake_client.invoke_calls[0]["body"]
    assert body["timeout"] == 5
    inner = shlex.split(body["command"])[2]  # the script handed to /bin/bash -lc
    assert "cd '/mnt/workspace/a b' || exit 1" in inner


def test_exec_reports_sandbox_error_as_event(ui, fake_client):
    fake_client.invoke_results = [client_error("AccessDeniedException", "nope", 403)]
    resp = ui.post("/api/exec", json={"runtime_session_id": SID_RICK_DEFAULT, "command": "id"}, headers=UI_HEADERS)
    assert resp.status_code == 200
    data = dict(parse_sse(resp.text))
    assert "done" not in data
    assert data["error"]["code"] == "AccessDeniedException"
    assert "nope" in data["error"]["message"]


def test_exec_rejects_unknown_session(ui, fake_client):
    resp = ui.post("/api/exec", json={"runtime_session_id": "99999999-9999-4999-8999-999999999999", "command": "id"}, headers=UI_HEADERS)
    assert resp.status_code == 404
    assert fake_client.invoke_calls == []


@pytest.mark.parametrize("timeout", [0, -1, 601, 100000])
def test_exec_rejects_bad_timeout(ui, fake_client, timeout):
    resp = ui.post("/api/exec", json={"runtime_session_id": SID_RICK_DEFAULT, "command": "id", "timeout": timeout}, headers=UI_HEADERS)
    assert resp.status_code == 400
    assert "timeout must be between 1 and 600" in resp.json()["detail"]
    assert fake_client.invoke_calls == []


def test_exec_rejects_empty_and_oversized_command(ui, fake_client):
    resp = ui.post("/api/exec", json={"runtime_session_id": SID_RICK_DEFAULT, "command": "   "}, headers=UI_HEADERS)
    assert resp.status_code == 400
    resp = ui.post("/api/exec", json={"runtime_session_id": SID_RICK_DEFAULT, "command": "x" * 65537}, headers=UI_HEADERS)
    assert resp.status_code == 400
    assert "exceeds" in resp.json()["detail"]
    assert fake_client.invoke_calls == []


def test_post_requires_ui_header_and_local_host(ui, fake_client, seeded_table, fake_control, fake_logs):
    # no custom header -> a cross-site simple request shape -> refused before any AWS call
    resp = ui.post("/api/exec", json={"runtime_session_id": SID_RICK_DEFAULT, "command": "id"})
    assert resp.status_code == 403
    assert fake_client.invoke_calls == []
    # wrong Host (DNS rebinding) -> refused for every route
    with TestClient(ui.app, base_url="http://evil.example") as evil:
        assert evil.get("/api/sandboxes").status_code == 403
        assert evil.get("/").status_code == 403


def test_pause_marks_row_paused(ui, fake_client, seeded_table):
    resp = ui.post("/api/pause", json={"runtime_session_id": SID_RICK_DEFAULT}, headers=UI_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "stopped"
    assert body["workspace"] == "default"
    assert body["status"] == "paused"
    assert fake_client.stop_calls[0]["runtimeSessionId"] == SID_RICK_DEFAULT
    item = seeded_table.get_item(Key={"user_sub": "sub-rick", "workspace": "default"})["Item"]
    assert item["status"] == "paused"
    # a second stop on an already-stopped session is reported, not raised
    fake_client.stop_results = [client_error("ResourceNotFoundException", "gone", 404, op="StopRuntimeSession")]
    assert ui.post("/api/pause", json={"runtime_session_id": SID_RICK_DEFAULT}, headers=UI_HEADERS).json()["result"] == "already_stopped"


def test_pause_unknown_session(ui, fake_client):
    resp = ui.post("/api/pause", json={"runtime_session_id": "nope"}, headers=UI_HEADERS)
    assert resp.status_code == 404
    assert fake_client.stop_calls == []


def _split_like_cloudwatch(ts: int, stream: str, rid: str, script: str) -> list[dict[str, Any]]:
    """The runtime logs one multi-line message; CloudWatch stores each line as its own event with
    the same timestamp and stream (observed shape in /aws/bedrock-agentcore/runtimes/...-DEFAULT)."""
    lines = script.split("\n")
    out = [{"timestamp": ts, "logStreamName": stream, "message": f"[{ts}] awsRequestId={rid} command={lines[0]}"}]
    out.extend({"timestamp": ts, "logStreamName": stream, "message": line} for line in lines[1:])
    return out


def test_activity_reassembles_split_command_lines(ui, fake_logs):
    home = "/mnt/workspace/.home"
    t0 = 1_760_000_000_000
    vm1 = "2026/09/11/[runtime-logs]aaaa"
    vm2 = "2026/09/11/[runtime-logs]bbbb"
    page1 = (
        _split_like_cloudwatch(t0, vm1, "req-1", build_script("echo hi && ls", "/mnt/workspace", home))
        + [{"timestamp": t0 + 500, "logStreamName": vm1, "message": f"[{t0 + 500}] awsRequestId=req-x some other log line"}]
        # two microVMs interleaved at the same instant: continuation lines must attach per stream
        + [
            _split_like_cloudwatch(t0 + 5000, vm2, "req-2", build_script("pwd", "/mnt/workspace/a b", home))[0],
            _split_like_cloudwatch(t0 + 5000, vm1, "req-3", build_script("echo 'multi\nline' | wc -l", "/mnt/workspace", home))[0],
        ]
    )
    rest2 = _split_like_cloudwatch(t0 + 5000, vm2, "req-2", build_script("pwd", "/mnt/workspace/a b", home))[1:]
    rest3 = _split_like_cloudwatch(t0 + 5000, vm1, "req-3", build_script("echo 'multi\nline' | wc -l", "/mnt/workspace", home))[1:]
    page2 = [rest2[0], rest3[0], rest2[1], rest3[1], rest3[2]] + [
        {"timestamp": t0 + 9000, "logStreamName": vm2, "message": f"[{t0 + 9000}] awsRequestId=req-4 command=/bin/bash -c 'not wrapped'"},
    ]
    fake_logs.pages = [{"events": page1, "nextToken": "more"}, {"events": page2}]

    body = ui.get("/api/activity?minutes=30&limit=10").json()
    assert body["log_group"] == f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT"
    assert body["minutes"] == 30
    got = [(e["request_id"], e["command"], e["working_dir"], e["log_stream"]) for e in body["events"]]
    assert got == [  # newest first; noise dropped; wrapper stripped; multi-line user command intact; non-wrapped verbatim
        ("req-4", "/bin/bash -c 'not wrapped'", None, vm2),
        ("req-2", "pwd", "/mnt/workspace/a b", vm2),
        ("req-3", "echo 'multi\nline' | wc -l", "/mnt/workspace", vm1),
        ("req-1", "echo hi && ls", "/mnt/workspace", vm1),
    ]
    assert body["events"][0]["time"].startswith("2025-10-09")
    # paginated on nextToken, scoped to the runtime's log group with a start time, no filterPattern
    assert len(fake_logs.calls) == 2
    assert fake_logs.calls[0]["logGroupName"] == body["log_group"]
    assert "startTime" in fake_logs.calls[0] and "filterPattern" not in fake_logs.calls[0]
    assert fake_logs.calls[1]["nextToken"] == "more"


def test_activity_single_event_message_and_unterminated_tail():
    wrapped = build_script("echo hi", "/mnt/workspace", "/mnt/workspace/.home")
    one = ui_app.parse_activity_event({"timestamp": 1_760_000_000_000, "logStreamName": "s", "message": f"[x] awsRequestId=r1 command={wrapped}"})
    assert one is not None and (one["command"], one["working_dir"], one["request_id"]) == ("echo hi", "/mnt/workspace", "r1")
    # a command whose closing line never arrived (window boundary) is still reported with what we have
    head = wrapped.split("\n")[0]
    tail = ui_app.assemble_commands([{"timestamp": 1, "logStreamName": "s", "message": f"[1] awsRequestId=r2 command={head}"}])
    assert len(tail) == 1 and tail[0]["request_id"] == "r2" and tail[0]["command"].startswith("/bin/bash -lc")


def test_activity_limit_and_missing_log_group(ui, fake_logs):
    fake_logs.pages = [{"events": [
        {"timestamp": 1_760_000_000_000 + i, "message": f"awsRequestId=r{i} command=echo {i}"} for i in range(5)
    ]}]
    body = ui.get("/api/activity?limit=2").json()
    assert [e["command"] for e in body["events"]] == ["echo 4", "echo 3"]

    class MissingGroup(FakeLogs):
        def filter_log_events(self, **kwargs: Any) -> dict[str, Any]:
            raise client_error("ResourceNotFoundException", "The specified log group does not exist.", 400, op="FilterLogEvents")

    ui.app.state.services.logs = MissingGroup()  # the route closure holds this same Services object
    body = ui.get("/api/activity").json()
    assert body["events"] == []
    assert "log group not found" in body["error"]


def test_unwrap_script_edge_cases():
    assert ui_app.unwrap_script("plain text") == ("plain text", None)
    assert ui_app.unwrap_script("unbalanced 'quote") == ("unbalanced 'quote", None)
    cmd, wd = ui_app.unwrap_script(build_script("cd /tmp || exit 1\nls", "/mnt/workspace"))  # user's own cd line survives
    assert cmd == "cd /tmp || exit 1\nls" and wd == "/mnt/workspace"
