"""Front-door MCP router.

Terminates MCP (stateful), owns the bash_exec tool, places each session on a
node-agent, records the binding as a Session CR, and forwards execution to the
pinned node's /exec over plain HTTP. Leader-elected; only the leader is Ready.

Run:  uv run python -m proxy.router --port 8080
"""
import asyncio
import json
import logging
import os
import socket as _socket
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field, ConfigDict

from . import k8s
from .leader import LeaderElector
from .placement import NodeRegistry, Placer, NoCapacity
from .session_cache import SessionCache

log = logging.getLogger("fc_mcp.router")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

POD_NAME = os.environ.get("POD_NAME", _socket.gethostname())
LEASE_NAME = os.environ.get("FC_MCP_LEASE", "fc-mcp-router-leader")
NODE_EXEC_PORT = int(os.environ.get("FC_MCP_NODE_PORT", "8080"))
HEARTBEAT_TIMEOUT = int(os.environ.get("FC_MCP_HEARTBEAT_TIMEOUT", "30"))  # ~3 missed beats

# Wired during lifespan.
_kc: Optional[k8s.K8sClient] = None
_registry: Optional[NodeRegistry] = None
_cache: Optional[SessionCache] = None
_placer: Optional[Placer] = None
_elector: Optional[LeaderElector] = None
_http: Optional[httpx.AsyncClient] = None


def _now():
    return datetime.now(timezone.utc)


# ─── MCP tool ───────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "firecracker_bash_mcp",
    # LOAD-BEARING: stateful + SSE so the SDK assigns and echoes Mcp-Session-Id,
    # which is the routing key. (See server.py for the same invariant.)
    stateless_http=False,
    json_response=False,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


class BashExecInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    command: str = Field(..., description="Shell command to execute (runs as root)", min_length=1, max_length=8192)
    timeout: int = Field(default=60, description="Max seconds to wait", ge=1, le=600)
    working_dir: Optional[str] = Field(default=None, description="Working directory inside the VM")


def _session_id(ctx: Context) -> Optional[str]:
    req = ctx.request_context.request
    return req.headers.get("mcp-session-id") if req else None


@mcp.tool(
    name="bash_exec",
    annotations={
        "title": "Execute a bash command in a microVM",
        "readOnlyHint": False, "destructiveHint": False,
        "idempotentHint": False, "openWorldHint": False,
    },
)
async def bash_exec(params: BashExecInput, ctx: Context) -> str:
    """Run a shell command as root inside this session's Firecracker microVM.

    The VM is placed on a node on first use and pinned there; subsequent calls
    reuse it (auto-resuming if it was paused). Returns JSON with stdout, stderr,
    returncode and elapsed_seconds.
    """
    sid = _session_id(ctx)
    if not sid:
        return json.dumps({"error": "no Mcp-Session-Id on request; client must initialize first"})

    # Resolve the pinned node (or place a new session).
    node = await _cache.get_node(sid)
    if not node:
        if not _elector.is_leader:
            return json.dumps({"error": "router not ready (leader election in progress); retry"})
        try:
            node = await _placer.place(sid)
            _cache.put(sid, node)
        except NoCapacity:
            return json.dumps({"error": "fleet at capacity: no node has a free VM slot"})

    addr = _registry.address(node)
    if not addr:
        await _registry.refresh()
        addr = _registry.address(node)
    if not addr:
        # The pinned node has no live agent — treat the session as lost.
        await _mark_lost(sid, node, "pinned node-agent is unreachable")
        return json.dumps({"error": "the node hosting this session is unavailable; reconnect to get a fresh environment"})

    payload = {"session_id": sid, "command": params.command,
               "working_dir": params.working_dir, "timeout": params.timeout}
    try:
        resp = await _http.post(f"http://{addr}:{NODE_EXEC_PORT}/exec", json=payload,
                                timeout=params.timeout + 30)
        return resp.text
    except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout):
        await _mark_lost(sid, node, "lost connection to the pinned node-agent")
        return json.dumps({"error": "the VM hosting this session was lost when its node failed; reconnect for a fresh environment"})
    except httpx.HTTPError as e:
        return json.dumps({"error": f"exec forwarding failed: {e}"})


async def _mark_lost(sid: str, node: str, reason: str):
    log.warning(f"session {sid} on node {node} lost: {reason}")
    _cache.drop(sid)
    try:
        await _kc.set_session_status(sid, phase="Lost", message=reason)
    except Exception as e:
        log.warning(f"could not mark session {sid} Lost: {e}")


# ─── leader-only background loops ────────────────────────────────────────────────

async def _liveness_sweep():
    """Mark NodeAgents whose heartbeat is stale Gone, and their sessions Lost.

    Wall-clock based (compares heartbeatTime to now), so a freshly-elected leader
    does not reset every node's timer and mass-flap agents to Gone.
    """
    while True:
        try:
            await asyncio.sleep(HEARTBEAT_TIMEOUT / 2)
            if not _elector.is_leader:
                continue
            now = _now()
            for a in await _kc.list_nodeagents():
                status = a.get("status") or {}
                node = a.get("spec", {}).get("nodeName")
                hb = status.get("heartbeatTime")
                stale = True
                if hb:
                    try:
                        stale = (now - datetime.fromisoformat(hb.replace("Z", "+00:00"))).total_seconds() > HEARTBEAT_TIMEOUT
                    except ValueError:
                        stale = True
                if stale and status.get("phase") != "Gone":
                    log.warning(f"node-agent {node} heartbeat stale -> Gone")
                    await _kc.set_nodeagent_phase(a["metadata"]["name"], "Gone")
                    for s in await _kc.sessions_on_node(node):
                        sid = s.get("spec", {}).get("mcpSessionId")
                        if sid and (s.get("status") or {}).get("phase") != "Lost":
                            await _kc.set_session_status(sid, phase="Lost", message="node failed")
                            _cache.drop(sid)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"liveness sweep error: {e}")


# ─── FastAPI (health / metrics) ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _kc, _registry, _cache, _placer, _elector, _http
    await k8s.load_config()
    _kc = k8s.K8sClient()
    _http = httpx.AsyncClient()
    _registry = NodeRegistry(_kc)
    _cache = SessionCache(_kc)
    _placer = Placer(_kc, _registry)
    _elector = LeaderElector(_kc._api_client, LEASE_NAME, POD_NAME)
    await _registry.start()
    await _cache.start()
    await _elector.start()
    sweep = asyncio.create_task(_liveness_sweep())
    log.info(f"router started as {POD_NAME}; namespace={k8s.NAMESPACE}")
    async with mcp._session_manager.run():
        try:
            yield
        finally:
            sweep.cancel()
            await _elector.stop()
            await _cache.stop()
            await _registry.stop()
            await _http.aclose()
            await _kc.close()


_mcp_starlette = mcp.streamable_http_app()
_mcp_handler = _mcp_starlette.routes[0].endpoint

api = FastAPI(title="Firecracker MCP Router", version="1.0.0", lifespan=lifespan)


@api.get("/healthz")
async def healthz():
    return {"status": "ok", "pod": POD_NAME}


@api.get("/readyz")
async def readyz():
    """Ready ONLY when this replica is the leader and its cache is synced.

    The router Service uses this probe, so traffic flows to a single active
    replica; standbys stay out of rotation until they win the Lease.
    """
    leader = bool(_elector and _elector.is_leader)
    synced = bool(_cache and _cache.synced)
    ok = leader and synced
    return JSONResponse(status_code=200 if ok else 503,
                        content={"ready": ok, "leader": leader, "cache_synced": synced})


@api.get("/metrics")
async def metrics():
    leader = 1 if (_elector and _elector.is_leader) else 0
    cached = len(_cache._by_id) if _cache else 0
    lines = [
        "# TYPE fcmcp_router_leader gauge",
        f"fcmcp_router_leader {leader}",
        "# TYPE fcmcp_router_cached_sessions gauge",
        f"fcmcp_router_cached_sessions {cached}",
    ]
    return PlainTextResponse("\n".join(lines) + "\n")


# ─── ASGI combiner: /mcp -> FastMCP, everything else -> FastAPI ───────────────────

class RouterASGI:
    def __init__(self, api_app, mcp_handler):
        self.api_app = api_app
        self.mcp_handler = mcp_handler

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").rstrip("/") == "/mcp":
            scope = dict(scope)
            scope["headers"] = [
                (k, v) for k, v in scope.get("headers", []) if k.lower() != b"host"
            ] + [(b"host", b"localhost")]
            await self.mcp_handler(scope, receive, send)
        else:
            await self.api_app(scope, receive, send)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Firecracker MCP router")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    uvicorn.run(RouterASGI(api, _mcp_handler), host=args.host, port=args.port)
