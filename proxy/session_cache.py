"""Watch-backed cache of Session CRs so routing needs no per-request API read.

Read-through on a miss (never negative-cache a well-formed id) guarantees a
session bound moments ago is always resolvable, even before the watch event
for it has arrived on this replica.
"""
import asyncio
import logging
from typing import Dict, Optional

from kubernetes_asyncio import client, watch

from . import k8s

log = logging.getLogger("fc_mcp.router.cache")


class SessionCache:
    def __init__(self, kc: k8s.K8sClient):
        self._kc = kc
        self._by_id: Dict[str, str] = {}  # mcp_session_id -> nodeName
        self._synced = asyncio.Event()
        self._co = client.CustomObjectsApi(kc._api_client)
        self._task = None

    @property
    def synced(self) -> bool:
        return self._synced.is_set()

    async def start(self):
        self._task = asyncio.create_task(self._watch_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()

    def put(self, mcp_session_id: str, node_name: str):
        self._by_id[mcp_session_id] = node_name

    def drop(self, mcp_session_id: str):
        self._by_id.pop(mcp_session_id, None)

    async def get_node(self, mcp_session_id: str) -> Optional[str]:
        node = self._by_id.get(mcp_session_id)
        if node:
            return node
        obj = await self._kc.get_session(mcp_session_id)  # read-through
        if obj:
            node = obj.get("spec", {}).get("nodeName")
            if node:
                self._by_id[mcp_session_id] = node
            return node
        return None

    def _index(self, obj):
        spec = obj.get("spec", {})
        sid, node = spec.get("mcpSessionId"), spec.get("nodeName")
        if sid and node:
            self._by_id[sid] = node

    async def _watch_loop(self):
        while True:
            try:
                resp = await self._co.list_namespaced_custom_object(
                    k8s.GROUP, k8s.VERSION, k8s.NAMESPACE, k8s.SESSIONS)
                for obj in resp.get("items", []):
                    self._index(obj)
                self._synced.set()
                rv = resp.get("metadata", {}).get("resourceVersion")

                w = watch.Watch()
                async with w:
                    async for event in w.stream(
                        self._co.list_namespaced_custom_object,
                        k8s.GROUP, k8s.VERSION, k8s.NAMESPACE, k8s.SESSIONS,
                        resource_version=rv, timeout_seconds=300,
                    ):
                        obj = event["object"]
                        sid = obj.get("spec", {}).get("mcpSessionId")
                        if not sid:
                            continue
                        if event["type"] == "DELETED":
                            self.drop(sid)
                        else:
                            self._index(obj)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"session watch restarting: {e}")
                await asyncio.sleep(2)
