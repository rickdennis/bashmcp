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
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
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

# Agent definitions are node-local JSON stores; the router remembers which node owns each
# agent (lazily, self-healing on a miss by scanning Ready nodes) so it can route an agent's
# CRUD there and create a session on the agent's home node — where its local definition lives.
# Environments are broadcast to every Ready node, so an environment_id resolves on any node.
_agent_home: dict = {}  # agent_id -> nodeName
_env_home: dict = {}    # env_id -> nodeName


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


# ─── Agent-sessions REST forwarding (managed-agents shape, HA) ────────────────────
#
# The node-agents own the data plane (agent/environment JSON stores + session VMs). The
# router is the single front door: it forwards each agent's CRUD to the node that owns it
# (home node, resolved lazily and self-healing), and creates a session on ANY node with
# capacity by resolving the agent+environment definitions and passing them INLINE — so a
# session is not tied to where its agent/env were created. Session ops then route to the
# session's pinned node via the Session CR (keyed by the sesn_ id, same as bash_exec).

def _require_leader():
    if not (_elector and _elector.is_leader):
        return JSONResponse(status_code=503,
                            content={"error": "router not ready (leader election in progress); retry"})
    return None


def _ready_addrs():
    """[(nodeName, podIP, freeTaps)] for every Ready node-agent."""
    out = []
    for a in _registry.agents():
        status = a.get("status") or {}
        if status.get("phase") != "Ready":
            continue
        node = a.get("spec", {}).get("nodeName")
        addr = _registry.address(node)
        if node and addr:
            out.append((node, addr, int(status.get("freeTaps", 0))))
    return out


def _pick_node():
    """The Ready node with the most free taps (None if the fleet is at capacity)."""
    cands = sorted(_ready_addrs(), key=lambda t: t[2], reverse=True)
    if not cands or cands[0][2] <= 0:
        return None
    return cands[0][0], cands[0][1]


async def _resolve_agent_home(agent_id: str) -> Optional[str]:
    """Which node owns this agent. Cached; on a miss, scan Ready nodes (self-healing across
    a leader failover, since the cache is in-memory)."""
    node = _agent_home.get(agent_id)
    if node and _registry.address(node):
        return node
    for node, addr, _free in _ready_addrs():
        try:
            r = await _http.get(f"http://{addr}:{NODE_EXEC_PORT}/v1/agents/{agent_id}", timeout=10)
            if r.status_code == 200:
                _agent_home[agent_id] = node
                return node
        except httpx.HTTPError:
            continue
    return None


def _json(r: httpx.Response) -> JSONResponse:
    return JSONResponse(status_code=r.status_code, content=(r.json() if r.content else None))


async def _forward_agent(agent_id: str, method: str, subpath: str, body=None) -> JSONResponse:
    node = await _resolve_agent_home(agent_id)
    if not node:
        return JSONResponse(status_code=404, content={"error": f"agent '{agent_id}' not found"})
    addr = _registry.address(node)
    if not addr:
        return JSONResponse(status_code=503, content={"error": "the agent's home node is unavailable"})
    try:
        r = await _http.request(method, f"http://{addr}:{NODE_EXEC_PORT}/v1/agents/{agent_id}{subpath}",
                                json=body, timeout=30)
    except httpx.HTTPError as e:
        return JSONResponse(status_code=502, content={"error": f"forwarding failed: {e}"})
    return _json(r)


async def _forward_session(sid: str, method: str, subpath: str, body=None, timeout: float = 60) -> JSONResponse:
    node = await _cache.get_node(sid)
    if not node:
        return JSONResponse(status_code=404, content={"error": f"session '{sid}' not found"})
    addr = _registry.address(node)
    if not addr:
        return JSONResponse(status_code=503, content={"error": "the node hosting this session is unavailable"})
    try:
        r = await _http.request(method, f"http://{addr}:{NODE_EXEC_PORT}/v1/sessions/{sid}{subpath}",
                                json=body, timeout=timeout)
    except httpx.HTTPError as e:
        return JSONResponse(status_code=502, content={"error": f"forwarding failed: {e}"})
    return _json(r)


# ── agents ──
@api.post("/v1/agents")
async def r_agent_create(req: Request):
    if (g := _require_leader()): return g
    picked = _pick_node()
    if not picked:
        return JSONResponse(status_code=503, content={"error": "no Ready node with capacity"})
    node, addr = picked
    try:
        r = await _http.post(f"http://{addr}:{NODE_EXEC_PORT}/v1/agents", json=await req.json(), timeout=30)
    except httpx.HTTPError as e:
        return JSONResponse(status_code=502, content={"error": f"agent create forwarding failed: {e}"})
    if r.status_code < 300 and (aid := r.json().get("id")):
        _agent_home[aid] = node
    return _json(r)


@api.get("/v1/agents")
async def r_agent_list():
    if (g := _require_leader()): return g
    data, seen = [], set()
    for node, addr, _free in _ready_addrs():
        try:
            r = await _http.get(f"http://{addr}:{NODE_EXEC_PORT}/v1/agents", timeout=15)
            for a in r.json().get("data", []):
                if a.get("id") not in seen:
                    seen.add(a.get("id")); data.append(a); _agent_home[a.get("id")] = node
        except httpx.HTTPError:
            continue
    return {"data": data}


@api.get("/v1/agents/{agent_id}")
async def r_agent_get(agent_id: str):
    if (g := _require_leader()): return g
    return await _forward_agent(agent_id, "GET", "")


@api.post("/v1/agents/{agent_id}")
async def r_agent_update(agent_id: str, req: Request):
    if (g := _require_leader()): return g
    return await _forward_agent(agent_id, "POST", "", body=await req.json())


@api.get("/v1/agents/{agent_id}/versions")
async def r_agent_versions(agent_id: str):
    if (g := _require_leader()): return g
    return await _forward_agent(agent_id, "GET", "/versions")


@api.post("/v1/agents/{agent_id}/archive")
async def r_agent_archive(agent_id: str):
    if (g := _require_leader()): return g
    return await _forward_agent(agent_id, "POST", "/archive")


# ── environments (home-noded, like agents) ──
@api.post("/v1/environments")
async def r_env_create(req: Request):
    if (g := _require_leader()): return g
    picked = _pick_node()
    if not picked:
        return JSONResponse(status_code=503, content={"error": "no Ready node"})
    node, addr = picked
    try:
        r = await _http.post(f"http://{addr}:{NODE_EXEC_PORT}/v1/environments", json=await req.json(), timeout=30)
    except httpx.HTTPError as e:
        return JSONResponse(status_code=502, content={"error": f"environment create forwarding failed: {e}"})
    if r.status_code < 300 and (eid := r.json().get("id")):
        _env_home[eid] = node
    return _json(r)


@api.get("/v1/environments")
async def r_env_list():
    if (g := _require_leader()): return g
    data, seen = [], set()
    for node, addr, _free in _ready_addrs():
        try:
            r = await _http.get(f"http://{addr}:{NODE_EXEC_PORT}/v1/environments", timeout=15)
            for e in r.json().get("data", []):
                if e.get("id") not in seen:
                    seen.add(e.get("id")); data.append(e); _env_home[e.get("id")] = node
        except httpx.HTTPError:
            continue
    return {"data": data}


async def _resolve_env(eid: str):
    """Return (node, env_record) for an environment, scanning on a cache miss."""
    node = _env_home.get(eid)
    addr = _registry.address(node) if node else None
    if addr:
        try:
            r = await _http.get(f"http://{addr}:{NODE_EXEC_PORT}/v1/environments/{eid}", timeout=10)
            if r.status_code == 200:
                return node, r.json()
        except httpx.HTTPError:
            pass
    for node, addr, _free in _ready_addrs():
        try:
            r = await _http.get(f"http://{addr}:{NODE_EXEC_PORT}/v1/environments/{eid}", timeout=10)
            if r.status_code == 200:
                _env_home[eid] = node
                return node, r.json()
        except httpx.HTTPError:
            continue
    return None, None


@api.get("/v1/environments/{eid}")
async def r_env_get(eid: str):
    if (g := _require_leader()): return g
    _node, env = await _resolve_env(eid)
    if not env:
        return JSONResponse(status_code=404, content={"error": f"environment '{eid}' not found"})
    return env


@api.delete("/v1/environments/{eid}")
async def r_env_delete(eid: str):
    if (g := _require_leader()): return g
    node, _env = await _resolve_env(eid)
    if not node:
        return JSONResponse(status_code=404, content={"error": f"environment '{eid}' not found"})
    addr = _registry.address(node)
    try:
        r = await _http.delete(f"http://{addr}:{NODE_EXEC_PORT}/v1/environments/{eid}", timeout=15)
    except httpx.HTTPError as e:
        return JSONResponse(status_code=502, content={"error": f"forwarding failed: {e}"})
    _env_home.pop(eid, None)
    return _json(r)


# ── sessions ──
@api.post("/v1/sessions")
async def r_session_create(req: Request):
    if (g := _require_leader()): return g
    body = await req.json()
    agent_id = body.get("agent_id")
    if not agent_id:
        return JSONResponse(status_code=422, content={"error": "agent_id is required"})
    # Resolve the agent definition on its home node, and the environment if one was named.
    home = await _resolve_agent_home(agent_id)
    if not home:
        return JSONResponse(status_code=404, content={"error": f"agent '{agent_id}' not found"})
    haddr = _registry.address(home)
    try:
        ar = await _http.get(f"http://{haddr}:{NODE_EXEC_PORT}/v1/agents/{agent_id}", timeout=15)
    except httpx.HTTPError as e:
        return JSONResponse(status_code=502, content={"error": f"agent lookup failed: {e}"})
    if ar.status_code != 200:
        return _json(ar)
    body["agent"] = ar.json()  # inline definition (carries versions; node picks agent_version)
    if body.get("environment_id"):
        _node, env = await _resolve_env(body["environment_id"])
        if not env:
            return JSONResponse(status_code=404,
                                content={"error": f"environment '{body['environment_id']}' not found"})
        body["environment"] = env
    # Place the session on any node with capacity (definitions travel inline).
    picked = _pick_node()
    if not picked:
        return JSONResponse(status_code=503, content={"error": "fleet at capacity: no node has a free VM slot"})
    node, addr = picked
    try:
        r = await _http.post(f"http://{addr}:{NODE_EXEC_PORT}/v1/sessions", json=body, timeout=180)
    except httpx.HTTPError as e:
        return JSONResponse(status_code=502, content={"error": f"session create forwarding failed: {e}"})
    if r.status_code < 300 and (sid := r.json().get("id")):
        try:
            await _kc.create_session(sid, node, vm_ref=r.json().get("vm_id", ""))
            _cache.put(sid, node)
        except Exception as e:
            log.warning(f"recording agent-session CR {sid} failed: {e}")
    return _json(r)


@api.get("/v1/sessions")
async def r_session_list():
    if (g := _require_leader()): return g
    data = []
    for node, addr, _free in _ready_addrs():
        try:
            r = await _http.get(f"http://{addr}:{NODE_EXEC_PORT}/v1/sessions", timeout=15)
            data.extend(r.json().get("data", []))
        except httpx.HTTPError:
            continue
    return {"data": data}


@api.get("/v1/sessions/{sid}")
async def r_session_get(sid: str):
    if (g := _require_leader()): return g
    return await _forward_session(sid, "GET", "")


@api.post("/v1/sessions/{sid}")
async def r_session_update(sid: str, req: Request):
    if (g := _require_leader()): return g
    return await _forward_session(sid, "POST", "", body=await req.json())


@api.delete("/v1/sessions/{sid}")
async def r_session_delete(sid: str):
    if (g := _require_leader()): return g
    resp = await _forward_session(sid, "DELETE", "", timeout=60)
    try:
        await _kc.delete_session(sid)
    except Exception as e:
        log.warning(f"deleting session CR {sid} failed: {e}")
    _cache.drop(sid)
    return resp


@api.post("/v1/sessions/{sid}/events")
async def r_session_event(sid: str, req: Request):
    if (g := _require_leader()): return g
    return await _forward_session(sid, "POST", "/events", body=await req.json())


@api.get("/v1/sessions/{sid}/events")
async def r_session_events(sid: str):
    if (g := _require_leader()): return g
    return await _forward_session(sid, "GET", "/events", timeout=90)


@api.get("/v1/sessions/{sid}/usage")
async def r_session_usage(sid: str):
    if (g := _require_leader()): return g
    return await _forward_session(sid, "GET", "/usage", timeout=90)


@api.get("/v1/sessions/{sid}/events/stream")
async def r_session_stream(sid: str, from_offset: int = 0):
    if (g := _require_leader()): return g
    node = await _cache.get_node(sid)
    if not node:
        return JSONResponse(status_code=404, content={"error": f"session '{sid}' not found"})
    addr = _registry.address(node)
    if not addr:
        return JSONResponse(status_code=503, content={"error": "the node hosting this session is unavailable"})
    url = f"http://{addr}:{NODE_EXEC_PORT}/v1/sessions/{sid}/events/stream"

    async def gen():
        try:
            async with _http.stream("GET", url, params={"from_offset": from_offset}, timeout=None) as resp:
                async for chunk in resp.aiter_raw():
                    yield chunk
        except httpx.HTTPError as e:
            yield (f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n").encode()

    return StreamingResponse(gen(), media_type="text/event-stream")


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
