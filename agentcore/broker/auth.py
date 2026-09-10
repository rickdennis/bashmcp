"""Caller identity for the broker.

Two modes:

* ``runlayer`` (default, GitOps deployment on EKS behind a Runlayer PrivateLink shim):
  Runlayer's proxy mints a short-lived EdDSA JWT per request in ``X-Runlayer-Identity-Token``
  (Identity Forward). We verify it against Runlayer's JWKS, check issuer/audience/expiry, and
  read the user's id and email from its claims. Optionally a shared static bearer (injected by
  the shim as ``UPSTREAM_BEARER``) is required as well.

* ``agentcore-jwt`` (broker hosted on AgentCore Runtime with a customJWTAuthorizer):
  AgentCore already verified the Authorization JWT; we only decode its claims.
"""
from __future__ import annotations

import base64
import hmac
import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

import jwt

IDENTITY_HEADER = "x-runlayer-identity-token"
RUNLAYER_AUD_PREFIX = "runlayer:identity-forward:"


class AuthError(Exception):
    """The request carries no usable identity."""


@dataclass(frozen=True)
class Identity:
    sub: str
    username: str | None = None
    email: str | None = None
    subject_type: str | None = None

    @property
    def display(self) -> str:
        return self.email or self.username or self.sub


class Authenticator(Protocol):
    def identify(self, headers: Mapping[str, str]) -> Identity: ...


def get_header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _bearer(headers: Mapping[str, str]) -> str:
    auth = get_header(headers, "authorization")
    if not auth:
        raise AuthError("missing Authorization header")
    scheme, _, token = auth.strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError("Authorization header is not a Bearer token")
    return token.strip()


# --------------------------------------------------------------------------------------
# agentcore-jwt mode
# --------------------------------------------------------------------------------------

def _b64url_decode(segment: str) -> bytes:
    padded = segment + "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def decode_unverified(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("token is not a JWT")
    try:
        claims = json.loads(_b64url_decode(parts[1]))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AuthError("token payload is not valid JSON") from exc
    if not isinstance(claims, dict):
        raise AuthError("token payload is not an object")
    return claims


def identity_from_claims(claims: Mapping[str, Any], expected_issuer: str | None = None) -> Identity:
    if expected_issuer and claims.get("iss") != expected_issuer:
        raise AuthError("token issuer mismatch")
    sub = claims.get("sub")
    if not sub or not isinstance(sub, str):
        raise AuthError("token has no sub claim")
    username = claims.get("username") or claims.get("cognito:username") or claims.get("preferred_username")
    email = claims.get("email")
    return Identity(
        sub=sub,
        username=str(username) if username else None,
        email=str(email) if email else None,
        subject_type="user",
    )


def identity_from_headers(headers: Mapping[str, str], expected_issuer: str | None = None) -> Identity:
    return identity_from_claims(decode_unverified(_bearer(headers)), expected_issuer)


class AgentCoreJwtAuthenticator:
    """Trusts the Authorization JWT because AgentCore's JWT authorizer already verified it."""

    def __init__(self, expected_issuer: str | None = None):
        self.expected_issuer = expected_issuer

    def identify(self, headers: Mapping[str, str]) -> Identity:
        return identity_from_headers(headers, self.expected_issuer)


# --------------------------------------------------------------------------------------
# runlayer mode
# --------------------------------------------------------------------------------------

def identity_from_runlayer_claims(claims: Mapping[str, Any]) -> Identity:
    sub = claims.get("sub")
    if not sub or not isinstance(sub, str):
        raise AuthError("identity token has no sub claim")
    subject_type = claims.get("subject_type")
    email = claims.get("user_email")
    username = claims.get("agent_name") if subject_type == "agent" else None
    return Identity(
        sub=sub,
        username=str(username) if username else None,
        email=str(email) if email else None,
        subject_type=str(subject_type) if subject_type else None,
    )


class RunlayerIdentityAuthenticator:
    """Verifies Runlayer's Identity Forward JWT (EdDSA) against the tenant's JWKS.

    ``audience`` is ``runlayer:identity-forward:<connector-id>``; until the connector id is known
    the check falls back to requiring the ``runlayer:identity-forward:`` prefix.
    """

    def __init__(
        self,
        *,
        jwks_url: str,
        issuer: str | None,
        audience: str | None = None,
        shared_bearer: str | None = None,
        jwk_client: Any | None = None,
        leeway: int = 30,
    ):
        self.jwks_url = jwks_url
        self.issuer = issuer
        self.audience = audience
        self.shared_bearer = shared_bearer
        self.leeway = leeway
        self._jwk_client = jwk_client or jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600, timeout=10)

    def identify(self, headers: Mapping[str, str]) -> Identity:
        if self.shared_bearer:
            presented = _bearer(headers)
            if not hmac.compare_digest(presented.encode(), self.shared_bearer.encode()):
                raise AuthError("shared bearer mismatch")
        token = get_header(headers, IDENTITY_HEADER)
        if not token:
            raise AuthError(f"missing {IDENTITY_HEADER} header (enable Identity Forward on the Runlayer connector)")
        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
        except jwt.PyJWKClientError as exc:
            raise AuthError(f"identity token signing key not found: {exc}") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthError(f"malformed identity token: {exc}") from exc
        options = {"require": ["exp", "iat", "sub", "aud"], "verify_aud": self.audience is not None}
        try:
            claims = jwt.decode(
                token,
                key=signing_key.key,
                algorithms=["EdDSA"],
                audience=self.audience,
                issuer=self.issuer,
                options=options,
                leeway=self.leeway,
            )
        except jwt.InvalidTokenError as exc:
            raise AuthError(f"invalid identity token: {exc}") from exc
        if self.audience is None:
            aud = claims.get("aud")
            auds = [aud] if isinstance(aud, str) else list(aud or [])
            if not any(isinstance(a, str) and a.startswith(RUNLAYER_AUD_PREFIX) for a in auds):
                raise AuthError("identity token audience is not a Runlayer identity-forward audience")
        return identity_from_runlayer_claims(claims)


def headers_from_context(ctx: Any) -> Mapping[str, str]:
    try:
        request = ctx.request_context.request
    except (AttributeError, ValueError) as exc:
        raise AuthError("no HTTP request in MCP context") from exc
    if request is None:
        raise AuthError("no HTTP request in MCP context")
    return request.headers


def identity_from_context(ctx: Any, expected_issuer: str | None = None) -> Identity:
    """agentcore-jwt convenience used by older callers/tests."""
    return identity_from_headers(headers_from_context(ctx), expected_issuer)
