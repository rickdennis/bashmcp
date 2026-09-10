"""Fixtures: fake AgentCore data-plane client, moto DynamoDB table, unsigned JWTs, fake MCP context."""
from __future__ import annotations

import base64
import json
import os
from typing import Any, Iterable

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

SANDBOX_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/bashmcp_sandbox-abc1234567"
ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST00000"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_jwt(**claims: Any) -> str:
    claims.setdefault("iss", ISSUER)
    header = b64url(json.dumps({"alg": "RS256", "kid": "test"}).encode())
    payload = b64url(json.dumps(claims).encode())
    return f"{header}.{payload}.signature"


def client_error(code: str, message: str = "", status: int = 400, op: str = "InvokeAgentRuntimeCommand") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message or code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        op,
    )


def events(stdout: str = "", stderr: str = "", exit_code: int | None = 0, status: str = "COMPLETED") -> list[dict]:
    stop: dict[str, Any] = {"status": status}
    if exit_code is not None:
        stop["exitCode"] = exit_code
    out: list[dict] = [{"chunk": {"contentStart": {}}}]
    if stdout:
        out.append({"chunk": {"contentDelta": {"stdout": stdout}}})
    if stderr:
        out.append({"chunk": {"contentDelta": {"stderr": stderr}}})
    out.append({"chunk": {"contentStop": stop}})
    return out


class FakeAgentCoreClient:
    """Scripted stand-in for boto3's bedrock-agentcore client (moto has no AgentCore support)."""

    def __init__(self, invoke_results: Iterable[Any] | None = None, stop_results: Iterable[Any] | None = None):
        self.invoke_results = list(invoke_results or [])
        self.stop_results = list(stop_results or [])
        self.invoke_calls: list[dict[str, Any]] = []
        self.stop_calls: list[dict[str, Any]] = []

    def invoke_agent_runtime_command(self, **kwargs: Any) -> dict[str, Any]:
        self.invoke_calls.append(kwargs)
        result = self.invoke_results.pop(0) if self.invoke_results else events("")
        if isinstance(result, Exception):
            raise result
        return {"stream": iter(result)}

    def stop_runtime_session(self, **kwargs: Any) -> dict[str, Any]:
        self.stop_calls.append(kwargs)
        result = self.stop_results.pop(0) if self.stop_results else {}
        if isinstance(result, Exception):
            raise result
        return result


class FakeRequest:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers


class FakeRequestContext:
    def __init__(self, request: Any):
        self.request = request


class FakeContext:
    def __init__(self, request: Any):
        self.request_context = FakeRequestContext(request)


def make_ctx(token: str | None = None, headers: dict[str, str] | None = None) -> FakeContext:
    hdrs = dict(headers or {})
    if token is not None:
        hdrs["authorization"] = f"Bearer {token}"
    return FakeContext(FakeRequest(hdrs))


@pytest.fixture
def ddb_table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="bashmcp-sandboxes",
            KeySchema=[
                {"AttributeName": "user_sub", "KeyType": "HASH"},
                {"AttributeName": "workspace", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_sub", "AttributeType": "S"},
                {"AttributeName": "workspace", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        yield table


@pytest.fixture
def fake_client() -> FakeAgentCoreClient:
    return FakeAgentCoreClient()


@pytest.fixture
def services(ddb_table, fake_client):
    from broker import app as broker_app
    from broker.auth import AgentCoreJwtAuthenticator
    from broker.config import Settings
    from broker.executor import SandboxExecutor
    from broker.registry import SandboxRegistry

    settings = Settings(sandbox_arn=SANDBOX_ARN, table_name="bashmcp-sandboxes", region="us-east-1",
                        expected_issuer=ISSUER, idle_timeout=1800, auth_mode="agentcore-jwt")
    executor = SandboxExecutor(fake_client, SANDBOX_ARN, conflict_max_wait=5, sleep=lambda _s: None)
    svc = broker_app.Services(settings=settings, registry=SandboxRegistry(ddb_table), executor=executor,
                              authenticator=AgentCoreJwtAuthenticator(expected_issuer=ISSUER))
    broker_app.set_services(svc)
    yield svc
    broker_app.set_services(None)
