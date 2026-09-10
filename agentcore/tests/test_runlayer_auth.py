"""RunlayerIdentityAuthenticator: EdDSA identity tokens verified against a (fake) JWKS."""
from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from broker import app
from broker.auth import AuthError, RunlayerIdentityAuthenticator
from broker.config import Settings
from tests.conftest import SANDBOX_ARN, FakeContext, FakeRequest, events

RUNLAYER = "https://stoneridge.runlayer.com"
SERVER_ID = "48afcafc-af9f-4512-be21-dd2809ba4ff9"
AUD = f"runlayer:identity-forward:{SERVER_ID}"


class KeyPair:
    def __init__(self, kid: str = "k1"):
        self.kid = kid
        self.private = Ed25519PrivateKey.generate()
        self.public_pem = self.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.jwk = json.loads(jwt.algorithms.OKPAlgorithm.to_jwk(self.private.public_key()))
        self.jwk["kid"] = kid

    def mint(self, **overrides) -> str:
        now = int(time.time())
        claims = {
            "iss": RUNLAYER, "aud": AUD, "sub": "user-123", "subject_type": "user",
            "user_id": "user-123", "user_email": "rick@example.com", "organization_id": "org-1",
            "iat": now, "exp": now + 300, "jti": "j1",
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, self.private, algorithm="EdDSA", headers={"kid": self.kid})


class FakeJwkClient:
    def __init__(self, *pairs: KeyPair):
        self.keys = {p.kid: p for p in pairs}

    def get_signing_key_from_jwt(self, token: str):
        kid = jwt.get_unverified_header(token).get("kid")
        if kid not in self.keys:
            raise jwt.PyJWKClientError(f"Unable to find a signing key that matches: {kid!r}")
        return jwt.PyJWK.from_dict(self.keys[kid].jwk)


@pytest.fixture
def keypair() -> KeyPair:
    return KeyPair()


def make_auth(keypair: KeyPair, **kw) -> RunlayerIdentityAuthenticator:
    kw.setdefault("issuer", RUNLAYER)
    return RunlayerIdentityAuthenticator(jwks_url=f"{RUNLAYER}/.well-known/x.json", jwk_client=FakeJwkClient(keypair), **kw)


def test_valid_token_with_exact_audience(keypair):
    ident = make_auth(keypair, audience=AUD).identify({"x-runlayer-identity-token": keypair.mint()})
    assert (ident.sub, ident.email, ident.subject_type) == ("user-123", "rick@example.com", "user")
    assert ident.display == "rick@example.com"


def test_prefix_audience_when_connector_id_unknown(keypair):
    ident = make_auth(keypair).identify({"X-Runlayer-Identity-Token": keypair.mint()})
    assert ident.sub == "user-123"
    with pytest.raises(AuthError, match="audience"):
        make_auth(keypair).identify({"x-runlayer-identity-token": keypair.mint(aud="something:else")})


def test_wrong_audience_rejected(keypair):
    with pytest.raises(AuthError, match="invalid identity token"):
        make_auth(keypair, audience=AUD).identify({"x-runlayer-identity-token": keypair.mint(aud="runlayer:identity-forward:other")})


def test_expired_and_wrong_issuer_rejected(keypair):
    auth = make_auth(keypair, audience=AUD)
    with pytest.raises(AuthError):
        auth.identify({"x-runlayer-identity-token": keypair.mint(exp=int(time.time()) - 600)})
    with pytest.raises(AuthError):
        auth.identify({"x-runlayer-identity-token": keypair.mint(iss="https://evil.example")})


def test_wrong_key_and_unknown_kid_rejected(keypair):
    other = KeyPair(kid="k1")  # same kid, different key => signature failure
    with pytest.raises(AuthError, match="invalid identity token"):
        make_auth(keypair, audience=AUD).identify({"x-runlayer-identity-token": other.mint()})
    unknown = KeyPair(kid="k2")
    with pytest.raises(AuthError, match="signing key"):
        make_auth(keypair, audience=AUD).identify({"x-runlayer-identity-token": unknown.mint()})


def test_hs256_token_rejected(keypair):
    forged = jwt.encode({"sub": "x", "aud": AUD, "iss": RUNLAYER, "iat": 1, "exp": int(time.time()) + 60},
                        "secret", algorithm="HS256", headers={"kid": "k1"})
    with pytest.raises(AuthError):
        make_auth(keypair, audience=AUD).identify({"x-runlayer-identity-token": forged})


def test_missing_header_and_agent_subject(keypair):
    with pytest.raises(AuthError, match="Identity Forward"):
        make_auth(keypair).identify({})
    ident = make_auth(keypair).identify({"x-runlayer-identity-token": keypair.mint(
        subject_type="agent", sub="agent-9", agent_id="agent-9", agent_name="burn-e", user_email=None, user_id=None)})
    assert (ident.sub, ident.username, ident.email, ident.display) == ("agent-9", "burn-e", None, "burn-e")


def test_shared_bearer_required_when_configured(keypair):
    auth = make_auth(keypair, audience=AUD, shared_bearer="s3cret")
    headers = {"x-runlayer-identity-token": keypair.mint()}
    with pytest.raises(AuthError, match="Authorization"):
        auth.identify(headers)
    with pytest.raises(AuthError, match="shared bearer"):
        auth.identify({**headers, "authorization": "Bearer wrong"})
    assert auth.identify({**headers, "Authorization": "Bearer s3cret"}).sub == "user-123"


async def test_tools_in_runlayer_mode(ddb_table, fake_client, keypair):
    from broker.executor import SandboxExecutor
    from broker.registry import SandboxRegistry

    settings = Settings(sandbox_arn=SANDBOX_ARN, table_name="bashmcp-sandboxes", region="us-east-1",
                        auth_mode="runlayer", runlayer_audience=AUD)
    svc = app.Services(settings=settings, registry=SandboxRegistry(ddb_table),
                       executor=SandboxExecutor(fake_client, SANDBOX_ARN, sleep=lambda _s: None),
                       authenticator=make_auth(keypair, audience=AUD))
    app.set_services(svc)
    try:
        fake_client.invoke_results = [events("hi\n")]
        ctx = FakeContext(FakeRequest({"x-runlayer-identity-token": keypair.mint()}))
        out = json.loads(await app.bash_exec("echo hi", ctx=ctx))
        assert out["stdout"] == "hi\n"
        row = svc.registry.get("user-123", "default")
        assert row is not None and row.user_email == "rick@example.com"
        denied = json.loads(await app.sandbox_list(FakeContext(FakeRequest({}))))
        assert denied["error"].startswith("Unauthorized")
    finally:
        app.set_services(None)


def test_build_authenticator_and_settings_defaults(monkeypatch):
    from broker.config import load_settings
    monkeypatch.setenv("RUNLAYER_URL", "https://srhg.runlayer.com/")
    monkeypatch.delenv("AUTH_MODE", raising=False)
    s = load_settings()
    assert s.auth_mode == "runlayer"
    assert s.runlayer_jwks_url == "https://srhg.runlayer.com/.well-known/runlayer-identity-forward.jwks.json"
    assert s.runlayer_issuer == "https://srhg.runlayer.com"
    auth = app.build_authenticator(s)
    assert isinstance(auth, RunlayerIdentityAuthenticator)
    monkeypatch.setenv("AUTH_MODE", "agentcore-jwt")
    assert type(app.build_authenticator(load_settings())).__name__ == "AgentCoreJwtAuthenticator"
    monkeypatch.setenv("AUTH_MODE", "bogus")
    with pytest.raises(RuntimeError):
        load_settings()


def test_resolve_sandbox_arn_by_name():
    class FakeControl:
        def list_agent_runtimes(self, **kw):
            if kw.get("nextToken"):
                return {"agentRuntimes": [{"agentRuntimeName": "bashmcp_sandbox_nonprod", "agentRuntimeArn": "arn:x"}]}
            return {"agentRuntimes": [{"agentRuntimeName": "other", "agentRuntimeArn": "arn:o"}], "nextToken": "t"}

    class FakeSession:
        def client(self, name):
            assert name == "bedrock-agentcore-control"
            return FakeControl()

    s = Settings(sandbox_arn="", table_name="t", region="us-east-1", sandbox_runtime_name="bashmcp_sandbox_nonprod")
    assert app.resolve_sandbox_arn(FakeSession(), s) == "arn:x"
    assert app.resolve_sandbox_arn(FakeSession(), Settings(sandbox_arn="arn:given", table_name="t", region="r")) == "arn:given"
    with pytest.raises(RuntimeError, match="no AgentCore runtime named"):
        app.resolve_sandbox_arn(FakeSession(), Settings(sandbox_arn="", table_name="t", region="r", sandbox_runtime_name="nope"))
    with pytest.raises(RuntimeError, match="SANDBOX_ARN or SANDBOX_RUNTIME_NAME"):
        app.resolve_sandbox_arn(FakeSession(), Settings(sandbox_arn="", table_name="t", region="r"))
