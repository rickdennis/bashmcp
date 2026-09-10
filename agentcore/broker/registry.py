"""DynamoDB registry mapping (caller, workspace) -> AgentCore runtimeSessionId.

Replaces VMState + SessionVMMap from server.py. AgentCore has no API to list sessions, so
this table is the source of truth for which sandboxes exist. Rows carry a TTL matching the
14-day idle expiry of AgentCore session storage.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from .auth import Identity

STORAGE_IDLE_DAYS = 14
WORKSPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SandboxExists(Exception):
    def __init__(self, workspace: str):
        super().__init__(f"sandbox '{workspace}' already exists")
        self.workspace = workspace


class InvalidWorkspace(ValueError):
    pass


def new_session_id() -> str:
    """uuid4 is 36 chars: satisfies AgentCore's 33-char minimum and its charset."""
    return str(uuid.uuid4())


def validate_workspace(name: str) -> str:
    if not isinstance(name, str) or not WORKSPACE_RE.match(name):
        raise InvalidWorkspace(
            "workspace must be 1-64 chars of letters, digits, '.', '_' or '-' and start with a letter or digit"
        )
    return name


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _expires_at() -> int:
    return int((datetime.now(timezone.utc) + timedelta(days=STORAGE_IDLE_DAYS)).timestamp())


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


@dataclass
class Sandbox:
    user_sub: str
    workspace: str
    runtime_session_id: str
    status: str
    created_at: str
    last_used_at: str
    label: str | None = None
    user_email: str | None = None

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "Sandbox":
        return cls(
            user_sub=item["user_sub"],
            workspace=item["workspace"],
            runtime_session_id=item["runtime_session_id"],
            status=item.get("status", "unknown"),
            created_at=item.get("created_at", ""),
            last_used_at=item.get("last_used_at", ""),
            label=item.get("label"),
            user_email=item.get("user_email"),
        )

    def to_public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("user_sub")
        return data

    def idle_seconds(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        try:
            last = datetime.fromisoformat(self.last_used_at)
        except ValueError:
            return 0
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return max(0, int((now - last).total_seconds()))


class SandboxRegistry:
    def __init__(self, table: Any):
        self._table = table

    def _key(self, user_sub: str, workspace: str) -> dict[str, str]:
        return {"user_sub": user_sub, "workspace": workspace}

    def get(self, user_sub: str, workspace: str) -> Sandbox | None:
        resp = self._table.get_item(Key=self._key(user_sub, workspace), ConsistentRead=True)
        item = resp.get("Item")
        return Sandbox.from_item(item) if item else None

    def create(self, identity: Identity, workspace: str, label: str | None = None) -> Sandbox:
        validate_workspace(workspace)
        now = _now_iso()
        item: dict[str, Any] = {
            "user_sub": identity.sub,
            "workspace": workspace,
            "runtime_session_id": new_session_id(),
            "status": "new",
            "created_at": now,
            "last_used_at": now,
            "expires_at": _expires_at(),
        }
        if label:
            item["label"] = label
        if identity.email:
            item["user_email"] = identity.email
        if identity.username:
            item["user_name"] = identity.username
        try:
            self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(user_sub)")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise SandboxExists(workspace) from exc
            raise
        return Sandbox.from_item(item)

    def get_or_create(self, identity: Identity, workspace: str) -> tuple[Sandbox, bool]:
        validate_workspace(workspace)
        existing = self.get(identity.sub, workspace)
        if existing:
            return existing, False
        try:
            return self.create(identity, workspace), True
        except SandboxExists:
            row = self.get(identity.sub, workspace)
            if row is None:  # pragma: no cover - lost a race with a concurrent delete
                raise
            return row, False

    def touch(self, user_sub: str, workspace: str, status: str = "active") -> None:
        try:
            self._table.update_item(
                Key=self._key(user_sub, workspace),
                UpdateExpression="SET last_used_at = :t, #s = :s, expires_at = :e",
                ConditionExpression="attribute_exists(user_sub)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":t": _now_iso(), ":s": status, ":e": _expires_at()},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise

    def set_status(self, user_sub: str, workspace: str, status: str) -> None:
        try:
            self._table.update_item(
                Key=self._key(user_sub, workspace),
                UpdateExpression="SET #s = :s",
                ConditionExpression="attribute_exists(user_sub)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": status},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise

    def list_for_user(self, user_sub: str) -> list[Sandbox]:
        rows: list[Sandbox] = []
        kwargs: dict[str, Any] = {"KeyConditionExpression": Key("user_sub").eq(user_sub)}
        while True:
            resp = self._table.query(**kwargs)
            rows.extend(Sandbox.from_item({k: _plain(v) for k, v in item.items()}) for item in resp.get("Items", []))
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        rows.sort(key=lambda r: r.workspace)
        return rows

    def delete(self, user_sub: str, workspace: str) -> bool:
        resp = self._table.delete_item(Key=self._key(user_sub, workspace), ReturnValues="ALL_OLD")
        return "Attributes" in resp
