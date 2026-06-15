"""Thin async Kubernetes client for the router.

All cross-node control-plane state lives in two CRs (Session, NodeAgent) in
etcd via the Kube API — no Redis. This module wraps the handful of CRUD/watch
calls the router needs.

Cluster-only quirk worth knowing: patching a CR `/status` subresource must use
a merge patch (`application/merge-patch+json`); the strategic-merge default the
generated client may pick can 415/422 on CRDs. `_patch_status` forces it.
"""
import os
import logging
from typing import Any, Dict, List, Optional

from kubernetes_asyncio import client, config

log = logging.getLogger("fc_mcp.router.k8s")

GROUP = "fcmcp.io"
VERSION = "v1alpha1"
NAMESPACE = os.environ.get("FC_MCP_NAMESPACE", "fc-mcp")
SESSIONS = "sessions"
NODEAGENTS = "nodeagents"

SESSION_ID_LABEL = "fcmcp.io/mcp-session-id"
NODE_LABEL = "fcmcp.io/node"

MERGE_PATCH = "application/merge-patch+json"


async def load_config():
    """In-cluster config, falling back to a local kubeconfig for dev."""
    try:
        config.load_incluster_config()
        log.info("loaded in-cluster Kubernetes config")
    except config.ConfigException:
        await config.load_kube_config()
        log.info("loaded local kubeconfig")


def session_name(mcp_session_id: str) -> str:
    """A DNS-safe CR name derived from the session id (uuid4 hex is already safe)."""
    return f"s-{mcp_session_id.lower()}"[:253]


class K8sClient:
    def __init__(self):
        self._api_client = client.ApiClient()
        self._co = client.CustomObjectsApi(self._api_client)

    async def close(self):
        await self._api_client.close()

    # ── Sessions ──────────────────────────────────────────────────────────────

    async def get_session(self, mcp_session_id: str) -> Optional[Dict[str, Any]]:
        try:
            return await self._co.get_namespaced_custom_object(
                GROUP, VERSION, NAMESPACE, SESSIONS, session_name(mcp_session_id)
            )
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return None
            raise

    async def list_sessions(self, label_selector: Optional[str] = None) -> List[Dict[str, Any]]:
        resp = await self._co.list_namespaced_custom_object(
            GROUP, VERSION, NAMESPACE, SESSIONS, label_selector=label_selector
        )
        return resp.get("items", [])

    async def create_session(self, mcp_session_id: str, node_name: str, vm_ref: str = "") -> Dict[str, Any]:
        body = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "Session",
            "metadata": {
                "name": session_name(mcp_session_id),
                "labels": {SESSION_ID_LABEL: mcp_session_id, NODE_LABEL: node_name},
            },
            "spec": {"mcpSessionId": mcp_session_id, "nodeName": node_name},
        }
        obj = await self._co.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, SESSIONS, body)
        await self.set_session_status(mcp_session_id, phase="Bound", vm_ref=vm_ref)
        return obj

    async def set_session_status(self, mcp_session_id: str, *, phase: str,
                                 vm_ref: Optional[str] = None, message: Optional[str] = None):
        status: Dict[str, Any] = {"phase": phase}
        if vm_ref is not None:
            status["vmRef"] = vm_ref
        if message is not None:
            status["message"] = message
        await self._patch_status(SESSIONS, session_name(mcp_session_id), {"status": status})

    async def delete_session(self, mcp_session_id: str):
        try:
            await self._co.delete_namespaced_custom_object(
                GROUP, VERSION, NAMESPACE, SESSIONS, session_name(mcp_session_id)
            )
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise

    # ── NodeAgents ────────────────────────────────────────────────────────────

    async def list_nodeagents(self) -> List[Dict[str, Any]]:
        resp = await self._co.list_namespaced_custom_object(GROUP, VERSION, NAMESPACE, NODEAGENTS)
        return resp.get("items", [])

    async def set_nodeagent_phase(self, name: str, phase: str):
        await self._patch_status(NODEAGENTS, name, {"status": {"phase": phase}})

    async def sessions_on_node(self, node_name: str) -> List[Dict[str, Any]]:
        return await self.list_sessions(label_selector=f"{NODE_LABEL}={node_name}")

    # ── internals ─────────────────────────────────────────────────────────────

    async def _patch_status(self, plural: str, name: str, body: Dict[str, Any]):
        # CRD /status must use a merge patch; the strategic-merge default can 415/422.
        try:
            await self._co.patch_namespaced_custom_object_status(
                GROUP, VERSION, NAMESPACE, plural, name, body, _content_type=MERGE_PATCH
            )
        except TypeError:
            # Older client without the _content_type kwarg.
            await self._co.patch_namespaced_custom_object_status(
                GROUP, VERSION, NAMESPACE, plural, name, body
            )
