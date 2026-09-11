#!/usr/bin/env python3
"""Local operator UI for the bashmcp AgentCore sandboxes.

A small FastAPI app bound to 127.0.0.1 that shows every sandbox in the DynamoDB registry (all
users), the sandbox runtime's status, a recent-command feed from the runtime's CloudWatch log
group, and lets an operator run a command in (or pause) a selected sandbox with THEIR OWN AWS
credentials. Every InvokeAgentRuntimeCommand / StopRuntimeSession lands in CloudTrail under the
operator's identity.

  uv run ui/app.py [--profile P] [--region R] [--port 8787] [--table bashmcp-sandboxes]
                   [--runtime-name bashmcp_sandbox_nonprod] [--idle 1800]
  uv run python -m ui.app ...

Run it from the agentcore/ directory (that is where the uv project lives).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

# `uv run ui/app.py` puts ui/ on sys.path, not the project root; make `broker` importable.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402
from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from broker.awsauth import make_session  # noqa: E402
from broker.executor import SandboxError, SandboxExecutor, build_script  # noqa: E402

log = logging.getLogger("bashmcp.ui")

DEFAULT_PROFILE = "aws-sr-am-admins@sr-es-devops-nonprod"
DEFAULT_REGION = "us-east-1"
DEFAULT_TABLE = "bashmcp-sandboxes"
DEFAULT_RUNTIME_NAME = "bashmcp_sandbox_nonprod"
DEFAULT_IDLE_SECONDS = 1800
DEFAULT_PORT = 8787
DEFAULT_WORKING_DIR = "/mnt/workspace"
HOME_DIR = "/mnt/workspace/.home"
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
MAX_COMMAND_BYTES = 65536
HEARTBEAT_SECONDS = 2.0
ACTIVITY_MAX_EVENTS = 100
ACTIVITY_MAX_MINUTES = 7 * 24 * 60
ACTIVITY_MAX_PAGES = 20
UI_HEADER = "x-bashmcp-ui"  # non-simple request header: forces a CORS preflight, which we never answer
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

INDEX_HTML_PATH = Path(__file__).with_name("index.html")

BANNER = (
    "Operator access: commands run as root inside the selected user's sandbox using your AWS "
    "credentials; every call is recorded in CloudTrail."
)


# --------------------------------------------------------------------------------------------
# Services (built once at startup; tests inject fakes)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeRef:
    name: str
    id: str
    arn: str

    @property
    def log_group(self) -> str:
        # AgentCore writes runtime logs to /aws/bedrock-agentcore/runtimes/<runtimeId>-<endpoint>.
        # Only the DEFAULT endpoint is used by this project.
        return f"/aws/bedrock-agentcore/runtimes/{self.id}-DEFAULT"


@dataclass
class Services:
    table: Any  # boto3 DynamoDB Table resource
    executor: SandboxExecutor
    control: Any  # bedrock-agentcore-control client
    logs: Any  # CloudWatch Logs client
    runtime: RuntimeRef
    region: str
    profile: str | None
    table_name: str
    idle_seconds: int = DEFAULT_IDLE_SECONDS
    caller_arn: str | None = None
    working_dir: str = DEFAULT_WORKING_DIR
    home_dir: str = HOME_DIR
    max_timeout: int = MAX_TIMEOUT
    allowed_hosts: frozenset[str] = LOCAL_HOSTS


def runtime_id_from_arn(arn: str) -> str:
    return arn.rsplit("/", 1)[-1]


def resolve_runtime(control: Any, name: str) -> RuntimeRef:
    """Look the sandbox runtime up by name (ListAgentRuntimes, paginated on nextToken)."""
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = control.list_agent_runtimes(**kwargs)
        for rt in resp.get("agentRuntimes", []):
            if rt.get("agentRuntimeName") == name:
                arn = rt["agentRuntimeArn"]
                return RuntimeRef(name=name, id=rt.get("agentRuntimeId") or runtime_id_from_arn(arn), arn=arn)
        token = resp.get("nextToken")
        if not token:
            raise RuntimeError(f"no AgentCore runtime named {name!r} in this account/region")


def build_services(args: argparse.Namespace) -> Services:
    if args.profile:
        session = boto3.session.Session(profile_name=args.profile, region_name=args.region)
    else:
        session = make_session(args.region)  # AWS_PROFILE / default chain
    control = session.client("bedrock-agentcore-control")
    runtime = resolve_runtime(control, args.runtime_name)
    client = session.client(
        "bedrock-agentcore",
        config=Config(retries={"mode": "standard", "max_attempts": 5}, connect_timeout=10, read_timeout=MAX_TIMEOUT + 120),
    )
    caller_arn: str | None = None
    try:
        caller_arn = session.client("sts").get_caller_identity()["Arn"]
    except (ClientError, BotoCoreError) as exc:  # pragma: no cover - informational only
        log.warning("could not resolve caller identity: %s", exc)
    return Services(
        table=session.resource("dynamodb").Table(args.table),
        executor=SandboxExecutor(client, runtime.arn, conflict_max_wait=120),
        control=control,
        logs=session.client("logs"),
        runtime=runtime,
        region=args.region,
        profile=args.profile or os.environ.get("AWS_PROFILE"),
        table_name=args.table,
        idle_seconds=args.idle,
        caller_arn=caller_arn,
    )


# --------------------------------------------------------------------------------------------
# Registry helpers (read side is a full scan: the operator view needs every user's rows)
# --------------------------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """DynamoDB numbers come back as Decimal; make rows JSON-serialisable."""
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, set)):
        return [_plain(v) for v in value]
    return value


def scan_all(table: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(_plain(item) for item in resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return items


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def idle_seconds_of(item: dict[str, Any], now: datetime | None = None) -> int | None:
    last = _parse_iso(item.get("last_used_at"))
    if last is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, int((now - last).total_seconds()))


def infer_state(status: str | None, idle: int | None, idle_limit: int) -> str:
    """Same rule as deploy/list_sandboxes.py: an explicit pause wins; otherwise AgentCore stops the
    microVM after the runtime idle timeout, so long idle means 'likely stopped' (it resumes on the
    next bash_exec, with /mnt/workspace intact)."""
    if status == "paused":
        return "paused"
    if idle is None:
        return status or "unknown"
    return "likely stopped" if idle > idle_limit else "active"


def sandbox_view(item: dict[str, Any], idle_limit: int, now: datetime | None = None) -> dict[str, Any]:
    idle = idle_seconds_of(item, now)
    return {
        "user_email": item.get("user_email"),
        "user_name": item.get("user_name"),
        "user_sub": item.get("user_sub"),
        "workspace": item.get("workspace"),
        "state": infer_state(item.get("status"), idle, idle_limit),
        "status": item.get("status"),
        "idle_seconds": idle,
        "created_at": item.get("created_at"),
        "last_used_at": item.get("last_used_at"),
        "runtime_session_id": item.get("runtime_session_id"),
        "label": item.get("label"),
        "expires_at": item.get("expires_at"),
    }


def find_by_session(items: list[dict[str, Any]], runtime_session_id: str) -> dict[str, Any] | None:
    return next((it for it in items if it.get("runtime_session_id") == runtime_session_id), None)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def set_row_status(table: Any, item: dict[str, Any], status: str, *, touch: bool = False) -> None:
    """Update a row's status (and optionally last_used_at) guarded by attribute_exists, so a row
    the broker deleted meanwhile is not resurrected."""
    expr = "SET #s = :s"
    values: dict[str, Any] = {":s": status}
    if touch:
        expr += ", last_used_at = :t"
        values[":t"] = _now_iso()
    try:
        table.update_item(
            Key={"user_sub": item["user_sub"], "workspace": item["workspace"]},
            UpdateExpression=expr,
            ConditionExpression="attribute_exists(user_sub)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


# --------------------------------------------------------------------------------------------
# Activity feed: parse `command=` lines out of the runtime log group
# --------------------------------------------------------------------------------------------

_COMMAND_LINE_RE = re.compile(r"awsRequestId=(?P<rid>\S+)\s+command=(?P<cmd>.*)\Z", re.S)
_HOME_LINE_RE = re.compile(r"^mkdir -p \S+ 2>/dev/null; export HOME=\S+$")
_CD_LINE_RE = re.compile(r"^cd (?P<dir>.+) \|\| exit 1$")


def unwrap_script(script: str) -> tuple[str, str | None]:
    """Undo build_script(): return (user command, working_dir). Anything that is not a
    build_script wrapper is returned verbatim."""
    try:
        argv = shlex.split(script)
    except ValueError:
        return script, None
    if len(argv) != 3 or argv[:2] != ["/bin/bash", "-lc"]:
        return script, None
    working_dir: str | None = None
    body: list[str] = []
    in_wrapper = True  # the wrapper is an optional HOME line then exactly one cd line; the rest is the user's
    for line in argv[2].split("\n"):
        if in_wrapper:
            if _HOME_LINE_RE.match(line):
                continue
            match = _CD_LINE_RE.match(line)
            in_wrapper = False
            if match:
                try:
                    working_dir = shlex.split(match.group("dir"))[0]
                except (ValueError, IndexError):
                    working_dir = match.group("dir")
                continue
        body.append(line)
    return "\n".join(body), working_dir


_HEADER_LINE_RE = re.compile(r"^\[\d+\]\s+awsRequestId=")
ACTIVITY_MAX_CONTINUATION_LINES = 400


def _script_complete(lines: list[str]) -> bool:
    """build_script() wraps everything in one shlex-quoted argument, so the script parses only once
    its closing quote has arrived; a non-wrapped single line parses immediately."""
    try:
        shlex.split("\n".join(lines))
    except ValueError:
        return False
    return True


def _finish(pending: dict[str, Any]) -> dict[str, Any]:
    event = pending["event"]
    command, working_dir = unwrap_script("\n".join(pending["lines"]).strip())
    ts_ms = event.get("timestamp")
    when = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds") if ts_ms else None
    return {
        "time": when,
        "timestamp_ms": ts_ms,
        "request_id": pending["request_id"],
        "command": command,
        "working_dir": working_dir,
        "log_stream": event.get("logStreamName"),
    }


def assemble_commands(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn raw log events into one record per command.

    The runtime logs `[ts] awsRequestId=<id> command=<script>` where <script> is multi-line;
    CloudWatch stores every line as its own event (same timestamp and stream). Events arrive in
    chronological order interleaved across streams (one stream per microVM), so continuation lines
    are attached per stream until the wrapped script's quoting balances, or the next header line
    starts a new record."""
    pending: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []

    def close(stream: str) -> None:
        if stream in pending:
            out.append(_finish(pending.pop(stream)))

    for event in events:
        stream = str(event.get("logStreamName") or "")
        message = str(event.get("message") or "").rstrip("\n")
        match = _COMMAND_LINE_RE.search(message)
        if match:
            close(stream)
            pending[stream] = {"event": event, "request_id": match.group("rid"), "lines": [match.group("cmd").rstrip()]}
            if _script_complete(pending[stream]["lines"]):
                close(stream)
            continue
        if stream not in pending:
            continue
        if _HEADER_LINE_RE.match(message) or len(pending[stream]["lines"]) >= ACTIVITY_MAX_CONTINUATION_LINES:
            close(stream)  # some other request's log line, or runaway: what we have is the command
            continue
        pending[stream]["lines"].append(message)
        if _script_complete(pending[stream]["lines"]):
            close(stream)
    for stream in list(pending):
        close(stream)
    return out


def parse_activity_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Single-event convenience (a whole `command=` message in one event); see assemble_commands."""
    records = assemble_commands([event])
    return records[0] if records else None


def fetch_activity(logs: Any, log_group: str, minutes: int, limit: int) -> list[dict[str, Any]]:
    start_ms = int((datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp() * 1000)
    raw: list[dict[str, Any]] = []
    # No filterPattern: the continuation lines of a command do not contain "command=".
    kwargs: dict[str, Any] = {"logGroupName": log_group, "startTime": start_ms}
    for _ in range(ACTIVITY_MAX_PAGES):
        resp = logs.filter_log_events(**kwargs)
        raw.extend(resp.get("events", []))
        token = resp.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token
    found = assemble_commands(raw)
    found.sort(key=lambda e: e.get("timestamp_ms") or 0, reverse=True)
    return found[:limit]


# --------------------------------------------------------------------------------------------
# HTTP app
# --------------------------------------------------------------------------------------------


class ExecRequest(BaseModel):
    runtime_session_id: str
    command: str
    working_dir: str | None = None
    timeout: int | None = None


class PauseRequest(BaseModel):
    runtime_session_id: str


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _aws_error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        return JSONResponse({"error": str(error.get("Message") or exc), "code": error.get("Code")}, status_code=502)
    return JSONResponse({"error": str(exc), "code": type(exc).__name__}, status_code=502)


def create_app(svc: Services) -> FastAPI:
    app = FastAPI(title="bashmcp sandbox operator UI", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = svc
    index_html = INDEX_HTML_PATH.read_text(encoding="utf-8")

    @app.middleware("http")
    async def local_only(request: Request, call_next):  # type: ignore[no-untyped-def]
        # Defence against DNS rebinding: a page on evil.example resolving to 127.0.0.1 would reach
        # us with Host: evil.example. Only answer for the loopback names we were bound on.
        if (request.url.hostname or "") not in svc.allowed_hosts:
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        if request.method == "POST" and request.headers.get(UI_HEADER) != "1":
            # A cross-site form/simple fetch cannot set a custom header; this forces a preflight
            # that we never grant, so no other origin can trigger exec/pause.
            return JSONResponse({"error": f"missing {UI_HEADER} header"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(ClientError)
    async def _client_error(_request: Request, exc: ClientError) -> JSONResponse:
        return _aws_error_response(exc)

    @app.exception_handler(BotoCoreError)
    async def _botocore_error(_request: Request, exc: BotoCoreError) -> JSONResponse:
        return _aws_error_response(exc)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(index_html)

    @app.get("/api/runtime")
    async def api_runtime() -> dict[str, Any]:
        rt = svc.runtime
        info: dict[str, Any] = {
            "name": rt.name,
            "id": rt.id,
            "arn": rt.arn,
            "region": svc.region,
            "profile": svc.profile,
            "caller_arn": svc.caller_arn,
            "log_group": rt.log_group,
            "table": svc.table_name,
            "idle_seconds": svc.idle_seconds,
            "status": None,
            "version": None,
            "last_updated": None,
            "description": None,
        }
        try:
            resp = await asyncio.to_thread(svc.control.get_agent_runtime, agentRuntimeId=rt.id)
        except (ClientError, BotoCoreError) as exc:
            info["status"] = "unknown"
            info["error"] = str(exc)
            return info
        updated = resp.get("lastUpdatedAt")
        info.update(
            status=resp.get("status"),
            version=resp.get("agentRuntimeVersion"),
            last_updated=updated.isoformat() if isinstance(updated, datetime) else updated,
            description=resp.get("description"),
        )
        return info

    @app.get("/api/sandboxes")
    async def api_sandboxes() -> dict[str, Any]:
        items = await asyncio.to_thread(scan_all, svc.table)
        now = datetime.now(timezone.utc)
        rows = [sandbox_view(it, svc.idle_seconds, now) for it in items]
        rows.sort(key=lambda r: ((r["user_email"] or r["user_sub"] or ""), r["workspace"] or ""))
        return {"sandboxes": rows, "count": len(rows), "table": svc.table_name, "idle_seconds": svc.idle_seconds}

    @app.get("/api/activity")
    async def api_activity(minutes: int = 60, limit: int = 50) -> dict[str, Any]:
        minutes = max(1, min(minutes, ACTIVITY_MAX_MINUTES))
        limit = max(1, min(limit, ACTIVITY_MAX_EVENTS))
        log_group = svc.runtime.log_group
        try:
            events = await asyncio.to_thread(fetch_activity, svc.logs, log_group, minutes, limit)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "ResourceNotFoundException":
                return {"events": [], "log_group": log_group, "minutes": minutes, "error": f"log group not found: {log_group}"}
            raise
        return {"events": events, "log_group": log_group, "minutes": minutes, "count": len(events)}

    async def _lookup(runtime_session_id: str) -> dict[str, Any]:
        if not runtime_session_id or not runtime_session_id.strip():
            raise HTTPException(status_code=400, detail="runtime_session_id is required")
        items = await asyncio.to_thread(scan_all, svc.table)
        item = find_by_session(items, runtime_session_id.strip())
        if item is None:
            raise HTTPException(status_code=404, detail="no sandbox with that runtime_session_id in the registry")
        return item

    @app.post("/api/exec")
    async def api_exec(body: ExecRequest) -> StreamingResponse:
        command = body.command
        if not command or not command.strip():
            raise HTTPException(status_code=400, detail="command must not be empty")
        if len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
            raise HTTPException(status_code=400, detail=f"command exceeds {MAX_COMMAND_BYTES} bytes")
        timeout = DEFAULT_TIMEOUT if body.timeout is None else body.timeout
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= svc.max_timeout:
            raise HTTPException(status_code=400, detail=f"timeout must be between 1 and {svc.max_timeout} seconds")
        working_dir = (body.working_dir or "").strip() or svc.working_dir
        item = await _lookup(body.runtime_session_id)
        sid = item["runtime_session_id"]
        script = build_script(command, working_dir, svc.home_dir)

        async def stream() -> AsyncIterator[str]:
            yield sse("start", {
                "runtime_session_id": sid,
                "workspace": item.get("workspace"),
                "user_email": item.get("user_email"),
                "working_dir": working_dir,
                "timeout": timeout,
            })
            started = time.monotonic()
            task = asyncio.ensure_future(asyncio.to_thread(svc.executor.run, sid, script, timeout))
            while not task.done():
                done, _pending = await asyncio.wait({task}, timeout=HEARTBEAT_SECONDS)
                if not done:
                    yield sse("heartbeat", {"elapsed_seconds": round(time.monotonic() - started, 1)})
            elapsed = round(time.monotonic() - started, 2)
            try:
                outcome = task.result()
            except SandboxError as exc:
                yield sse("error", {"code": exc.code, "message": exc.message, "elapsed_seconds": elapsed})
                return
            except (ClientError, BotoCoreError) as exc:
                yield sse("error", {"code": type(exc).__name__, "message": str(exc), "elapsed_seconds": elapsed})
                return
            # The microVM is running again after an exec: keep the registry's idle/status honest.
            try:
                await asyncio.to_thread(set_row_status, svc.table, item, "active", touch=True)
            except (ClientError, BotoCoreError) as exc:
                log.warning("could not touch registry row: %s", exc)
            yield sse("stdout", {"data": outcome.stdout})
            yield sse("stderr", {"data": outcome.stderr})
            yield sse("done", {
                "exit_code": outcome.exit_code,
                "status": outcome.status,
                "elapsed_seconds": elapsed,
                "attempts": outcome.attempts,
                "wait_seconds": outcome.wait_seconds,
            })

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/pause")
    async def api_pause(body: PauseRequest) -> dict[str, Any]:
        item = await _lookup(body.runtime_session_id)
        sid = item["runtime_session_id"]
        try:
            result = await asyncio.to_thread(svc.executor.stop, sid)
        except SandboxError as exc:
            raise HTTPException(status_code=502, detail={"code": exc.code, "message": exc.message}) from exc
        await asyncio.to_thread(set_row_status, svc.table, item, "paused")
        return {
            "result": result,
            "runtime_session_id": sid,
            "workspace": item.get("workspace"),
            "user_email": item.get("user_email"),
            "status": "paused",
        }

    return app


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default=None,
                        help=f"AWS profile (default: AWS_PROFILE / default credential chain; deploy scripts use {DEFAULT_PROFILE})")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--runtime-name", default=DEFAULT_RUNTIME_NAME, help="sandbox AgentCore runtime name (ARN is resolved)")
    parser.add_argument("--idle", type=int, default=DEFAULT_IDLE_SECONDS, help="runtime idle timeout in seconds (for the inferred state)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    args = parse_args(argv)
    # Access log only: uvicorn prints method + path + status; request bodies (command text) are never logged.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    svc = build_services(args)
    log.info("runtime %s (%s) table=%s region=%s profile=%s caller=%s",
             svc.runtime.name, svc.runtime.id, svc.table_name, svc.region, svc.profile or "<default chain>", svc.caller_arn)
    log.info("operator UI on http://127.0.0.1:%d  (loopback only)", args.port)
    uvicorn.run(create_app(svc), host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
