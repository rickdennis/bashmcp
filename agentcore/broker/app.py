"""bashmcp broker: MCP server (streamable HTTP, stateless) for AgentCore Runtime.

Tools mirror server.py's bash_exec plus the REST management surface, retargeted at
AgentCore: one persistent sandbox session per (caller, workspace).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from .awsauth import make_session
from .auth import (
    AgentCoreJwtAuthenticator,
    Authenticator,
    AuthError,
    Identity,
    RunlayerIdentityAuthenticator,
    headers_from_context,
)
from .config import MAX_COMMAND_BYTES, Settings, load_settings
from .executor import SandboxError, SandboxExecutor, build_script
from .models import ExecResult, error_json, ok_json
from .registry import InvalidWorkspace, SandboxExists, SandboxRegistry

log = logging.getLogger("bashmcp.broker")


@dataclass
class Services:
    settings: Settings
    registry: SandboxRegistry
    executor: SandboxExecutor
    authenticator: Authenticator


_services: Services | None = None


def set_services(services: Services | None) -> None:
    global _services
    _services = services


def get_services() -> Services:
    global _services
    if _services is None:
        _services = _build_services()
    return _services


def build_authenticator(settings: Settings) -> Authenticator:
    if settings.auth_mode == "agentcore-jwt":
        return AgentCoreJwtAuthenticator(expected_issuer=settings.expected_issuer)
    return RunlayerIdentityAuthenticator(
        jwks_url=settings.runlayer_jwks_url or "",
        issuer=settings.runlayer_issuer,
        audience=settings.runlayer_audience,
        shared_bearer=settings.shared_bearer,
    )


def resolve_sandbox_arn(session: boto3.session.Session, settings: Settings) -> str:
    """SANDBOX_ARN wins; otherwise look the runtime up by name so manifests need no generated ARN."""
    if settings.sandbox_arn:
        return settings.sandbox_arn
    if not settings.sandbox_runtime_name:
        raise RuntimeError("set SANDBOX_ARN or SANDBOX_RUNTIME_NAME (name of the sandbox AgentCore Runtime)")
    control = session.client("bedrock-agentcore-control")
    token = None
    while True:
        kwargs = {"maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = control.list_agent_runtimes(**kwargs)
        for rt in resp.get("agentRuntimes", []):
            if rt.get("agentRuntimeName") == settings.sandbox_runtime_name:
                return rt["agentRuntimeArn"]
        token = resp.get("nextToken")
        if not token:
            raise RuntimeError(f"no AgentCore runtime named {settings.sandbox_runtime_name!r} in {settings.region}")


def _build_services() -> Services:
    settings = load_settings()
    session = make_session(settings.region)
    sandbox_arn = resolve_sandbox_arn(session, settings)
    table = session.resource("dynamodb").Table(settings.table_name)
    client = session.client(
        "bedrock-agentcore",
        config=Config(
            retries={"mode": "standard", "max_attempts": 5},
            connect_timeout=10,
            read_timeout=settings.max_timeout + 120,
        ),
    )
    executor = SandboxExecutor(client, sandbox_arn, conflict_max_wait=settings.conflict_max_wait)
    return Services(
        settings=settings,
        registry=SandboxRegistry(table),
        executor=executor,
        authenticator=build_authenticator(settings),
    )


mcp = FastMCP(
    "bashmcp_broker",
    instructions=(
        "Remote bash for Claude Code. bash_exec runs a command as root inside your own persistent "
        "sandbox microVM; /mnt/workspace survives pauses. Use sandbox_* tools to manage workspaces."
    ),
    host="0.0.0.0",
    port=8000,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _identity(ctx: Context) -> Identity:
    return get_services().authenticator.identify(headers_from_context(ctx))


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request) -> JSONResponse:
    """Liveness/readiness for Kubernetes and the Envoy gateway health check. Never touches AWS."""
    return JSONResponse({"status": "ok", "service": "bashmcp-broker"})


def _not_found(workspace: str) -> str:
    return error_json(
        f"No sandbox named '{workspace}'. Run bash_exec (creates it on first use) or sandbox_new.",
        workspace=workspace,
    )


@mcp.tool(
    name="bash_exec",
    annotations={
        "title": "Execute a bash command in your AgentCore sandbox",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def bash_exec(
    command: str,
    ctx: Context,
    working_dir: str | None = None,
    timeout: int = 60,
    workspace: str = "default",
) -> str:
    """Run a shell command as root inside your persistent sandbox microVM.

    Args:
        command: Bash command to run (up to 64 KB). Each call is a fresh bash process;
            chain with && or ; if you need state, and keep files under /mnt/workspace,
            which is the only path that survives a pause.
        working_dir: Directory to cd into first (default /mnt/workspace).
        timeout: Max seconds to wait (default 60; server caps the maximum).
        workspace: Named sandbox to use (default "default"). Created on first use.

    Returns:
        JSON with stdout, stderr, returncode, elapsed_seconds, status and cold_start.
    """
    svc = get_services()
    settings = svc.settings
    try:
        identity = _identity(ctx)
    except AuthError as exc:
        return error_json(f"Unauthorized: {exc}")
    if not command or not command.strip():
        return error_json("command must not be empty")
    if len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
        return error_json(f"command exceeds {MAX_COMMAND_BYTES} bytes")
    if not isinstance(timeout, int) or not 1 <= timeout <= settings.max_timeout:
        return error_json(f"timeout must be between 1 and {settings.max_timeout} seconds")

    try:
        sandbox, created = await asyncio.to_thread(svc.registry.get_or_create, identity, workspace)
    except InvalidWorkspace as exc:
        return error_json(str(exc))

    script = build_script(command, working_dir or settings.default_working_dir, settings.home_dir)
    start = time.monotonic()
    try:
        outcome = await asyncio.to_thread(svc.executor.run, sandbox.runtime_session_id, script, timeout)
    except SandboxError as exc:
        log.warning("bash_exec failed user=%s workspace=%s error=%s", identity.display, workspace, exc)
        return error_json(
            f"Sandbox error: {exc}",
            workspace=workspace,
            runtime_session_id=sandbox.runtime_session_id,
        )
    elapsed = round(time.monotonic() - start, 2)
    await asyncio.to_thread(svc.registry.touch, identity.sub, workspace, "active")

    stderr = outcome.stderr
    if outcome.status == "COMPLETED":
        returncode = outcome.exit_code if outcome.exit_code is not None else -1
    else:
        returncode = -1
        if stderr and not stderr.endswith("\n"):
            stderr += "\n"
        stderr += "Command timed out"

    cold_start = created or sandbox.status in ("new", "paused") or outcome.attempts > 1
    log.info(
        "bash_exec user=%s workspace=%s rc=%s status=%s elapsed=%.2fs attempts=%d cold=%s cmd=%r",
        identity.display, workspace, returncode, outcome.status, elapsed, outcome.attempts, cold_start,
        command[:200],
    )
    return ExecResult(
        workspace=workspace,
        runtime_session_id=sandbox.runtime_session_id,
        command=command,
        stdout=outcome.stdout,
        stderr=stderr,
        returncode=returncode,
        elapsed_seconds=elapsed,
        status=outcome.status,
        cold_start=cold_start,
    ).to_json()


@mcp.tool(
    name="sandbox_list",
    annotations={"title": "List your sandboxes", "readOnlyHint": True, "openWorldHint": False},
)
async def sandbox_list(ctx: Context) -> str:
    """List the sandboxes (workspaces) that belong to you."""
    svc = get_services()
    try:
        identity = _identity(ctx)
    except AuthError as exc:
        return error_json(f"Unauthorized: {exc}")
    rows = await asyncio.to_thread(svc.registry.list_for_user, identity.sub)
    return ok_json(sandboxes=[r.to_public() for r in rows], count=len(rows))


@mcp.tool(
    name="sandbox_status",
    annotations={"title": "Show one sandbox", "readOnlyHint": True, "openWorldHint": False},
)
async def sandbox_status(ctx: Context, workspace: str = "default") -> str:
    """Show a sandbox's metadata and whether its microVM has probably been stopped for idleness."""
    svc = get_services()
    try:
        identity = _identity(ctx)
    except AuthError as exc:
        return error_json(f"Unauthorized: {exc}")
    row = await asyncio.to_thread(svc.registry.get, identity.sub, workspace)
    if row is None:
        return _not_found(workspace)
    idle = row.idle_seconds(datetime.now(timezone.utc))
    likely_stopped = row.status == "paused" or idle > svc.settings.idle_timeout
    return ok_json(
        **row.to_public(),
        idle_seconds=idle,
        likely_stopped=likely_stopped,
        note=(
            "AgentCore has no session status API; likely_stopped is inferred from idle time "
            f"(idle timeout {svc.settings.idle_timeout}s). A stopped sandbox resumes on the next bash_exec."
        ),
    )


@mcp.tool(
    name="sandbox_pause",
    annotations={"title": "Pause a sandbox", "readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
)
async def sandbox_pause(ctx: Context, workspace: str = "default") -> str:
    """Stop the sandbox's microVM now. /mnt/workspace is kept; processes are not. Resumes on next bash_exec."""
    svc = get_services()
    try:
        identity = _identity(ctx)
    except AuthError as exc:
        return error_json(f"Unauthorized: {exc}")
    row = await asyncio.to_thread(svc.registry.get, identity.sub, workspace)
    if row is None:
        return _not_found(workspace)
    try:
        result = await asyncio.to_thread(svc.executor.stop, row.runtime_session_id)
    except SandboxError as exc:
        return error_json(f"Sandbox error: {exc}", workspace=workspace, runtime_session_id=row.runtime_session_id)
    await asyncio.to_thread(svc.registry.set_status, identity.sub, workspace, "paused")
    return ok_json(workspace=workspace, runtime_session_id=row.runtime_session_id, status="paused", stop_result=result)


@mcp.tool(
    name="sandbox_new",
    annotations={"title": "Create a named sandbox", "readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
)
async def sandbox_new(ctx: Context, workspace: str, label: str | None = None) -> str:
    """Create a new, empty sandbox under the given workspace name."""
    svc = get_services()
    try:
        identity = _identity(ctx)
    except AuthError as exc:
        return error_json(f"Unauthorized: {exc}")
    try:
        row = await asyncio.to_thread(svc.registry.create, identity, workspace, label)
    except SandboxExists as exc:
        return error_json(str(exc), workspace=workspace)
    except InvalidWorkspace as exc:
        return error_json(str(exc), workspace=workspace)
    return ok_json(**row.to_public(), created=True)


@mcp.tool(
    name="sandbox_destroy",
    annotations={"title": "Destroy a sandbox", "readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
)
async def sandbox_destroy(ctx: Context, workspace: str = "default") -> str:
    """Stop the sandbox and forget it. Its storage is reclaimed by AgentCore after 14 idle days."""
    svc = get_services()
    try:
        identity = _identity(ctx)
    except AuthError as exc:
        return error_json(f"Unauthorized: {exc}")
    row = await asyncio.to_thread(svc.registry.get, identity.sub, workspace)
    if row is None:
        return _not_found(workspace)
    try:
        stop_result = await asyncio.to_thread(svc.executor.stop, row.runtime_session_id)
    except SandboxError as exc:
        stop_result = f"stop_failed: {exc.code}"
    await asyncio.to_thread(svc.registry.delete, identity.sub, workspace)
    return ok_json(
        workspace=workspace,
        runtime_session_id=row.runtime_session_id,
        status="destroyed",
        stop_result=stop_result,
        note="Session storage is deleted by AgentCore after 14 idle days; there is no per-session delete API.",
    )


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    services = get_services()
    log.info(
        "bashmcp broker starting auth_mode=%s sandbox=%s table=%s max_timeout=%s shared_bearer=%s",
        services.settings.auth_mode, services.executor._arn, services.settings.table_name,
        services.settings.max_timeout, bool(services.settings.shared_bearer),
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
