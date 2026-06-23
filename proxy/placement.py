"""Node registry + least-loaded placement (leader-only writes the Session CR)."""
import asyncio
import logging
from typing import Dict, List, Optional

from . import k8s

log = logging.getLogger("fc_mcp.router.placement")


class NoCapacity(Exception):
    """All node-agents are full or unavailable."""


class NodeRegistry:
    """Polls NodeAgent CRs; exposes node addresses and capacity."""

    def __init__(self, kc: k8s.K8sClient, refresh_interval: int = 10):
        self._kc = kc
        self._refresh_interval = refresh_interval
        self._agents: List[dict] = []
        self._addr: Dict[str, str] = {}  # nodeName -> podIP
        self._task = None

    async def start(self):
        await self.refresh()
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def _loop(self):
        while True:
            try:
                await asyncio.sleep(self._refresh_interval)
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"node registry refresh failed: {e}")

    async def refresh(self):
        agents = await self._kc.list_nodeagents()
        addr = {}
        for a in agents:
            spec = a.get("spec", {})
            node, ip = spec.get("nodeName"), spec.get("podIP")
            if node and ip:
                addr[node] = ip
        self._agents, self._addr = agents, addr

    def address(self, node_name: str) -> Optional[str]:
        return self._addr.get(node_name)

    def agents(self) -> List[dict]:
        return list(self._agents)


class Placer:
    def __init__(self, kc: k8s.K8sClient, registry: NodeRegistry):
        self._kc = kc
        self._reg = registry

    async def place(self, mcp_session_id: str) -> str:
        """Pick the Ready node with the most free taps, create the Session CR, return its node.

        Sessions still Pending (pinned but not yet reflected in the agent's reported
        freeTaps) are subtracted from each node's free count so two rapid placements
        cannot overshoot the 32-tap cap before the agent re-reports.
        """
        await self._reg.refresh()  # decide on fresh capacity
        pending_by_node: Dict[str, int] = {}
        for s in await self._kc.list_sessions():
            node = s.get("spec", {}).get("nodeName")
            phase = (s.get("status") or {}).get("phase")
            if node and phase in (None, "Pending"):
                pending_by_node[node] = pending_by_node.get(node, 0) + 1

        best, best_free = None, 0
        for a in self._reg.agents():
            status = a.get("status") or {}
            if status.get("phase") != "Ready":
                continue
            node = a.get("spec", {}).get("nodeName")
            free = int(status.get("freeTaps", 0)) - pending_by_node.get(node, 0)
            if free > best_free:
                best, best_free = node, free

        if not best:
            raise NoCapacity("no node-agent with free capacity")

        await self._kc.create_session(mcp_session_id, best)
        log.info(f"placed session {mcp_session_id} -> node {best} (free≈{best_free})")
        return best
