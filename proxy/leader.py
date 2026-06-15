"""Lease-based leader election (coordination.k8s.io/v1) — no Redis.

Only the leader reports Ready (see router.py), so the router Service routes
traffic to a single active replica. A standby acquires the Lease within
~lease_duration after the leader stops renewing.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

from kubernetes_asyncio import client

log = logging.getLogger("fc_mcp.router.leader")

NAMESPACE = os.environ.get("FC_MCP_NAMESPACE", "fc-mcp")


class LeaderElector:
    def __init__(self, api_client, lease_name: str, identity: str,
                 lease_duration: int = 15, renew_interval: int = 10):
        self.lease_name = lease_name
        self.identity = identity
        self.lease_duration = lease_duration
        self.renew_interval = renew_interval
        self._is_leader = False
        self._coord = client.CoordinationV1Api(api_client)
        self._task = None

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    def _set(self, leader: bool):
        if leader != self._is_leader:
            log.info(f"leadership {'ACQUIRED' if leader else 'LOST'} by {self.identity}")
        self._is_leader = leader

    async def _run(self):
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"leader election tick failed: {e}")
                self._set(False)
            await asyncio.sleep(self.renew_interval)

    async def _tick(self):
        try:
            lease = await self._coord.read_namespaced_lease(self.lease_name, NAMESPACE)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                await self._create_and_acquire()
                return
            raise

        spec = lease.spec
        now = self._now()
        if spec.holder_identity == self.identity:
            spec.renew_time = now
            await self._coord.replace_namespaced_lease(self.lease_name, NAMESPACE, lease)
            self._set(True)
            return

        renew = spec.renew_time
        expired = renew is None or (now - renew).total_seconds() > self.lease_duration
        if not expired:
            self._set(False)
            return

        spec.holder_identity = self.identity
        spec.acquire_time = now
        spec.renew_time = now
        spec.lease_transitions = (spec.lease_transitions or 0) + 1
        try:
            await self._coord.replace_namespaced_lease(self.lease_name, NAMESPACE, lease)
            self._set(True)
        except client.exceptions.ApiException as e:
            if e.status == 409:
                self._set(False)  # lost the race to another replica
            else:
                raise

    async def _create_and_acquire(self):
        now = self._now()
        body = client.V1Lease(
            metadata=client.V1ObjectMeta(name=self.lease_name, namespace=NAMESPACE),
            spec=client.V1LeaseSpec(
                holder_identity=self.identity,
                lease_duration_seconds=self.lease_duration,
                acquire_time=now, renew_time=now, lease_transitions=0,
            ),
        )
        try:
            await self._coord.create_namespaced_lease(NAMESPACE, body)
            self._set(True)
        except client.exceptions.ApiException as e:
            if e.status == 409:
                self._set(False)
            else:
                raise
