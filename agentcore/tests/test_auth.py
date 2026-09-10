from __future__ import annotations

import pytest

from broker.auth import AuthError, decode_unverified, identity_from_context, identity_from_headers
from tests.conftest import ISSUER, make_ctx, make_jwt


def test_decode_unverified_roundtrip():
    claims = decode_unverified(make_jwt(sub="abc", email="a@b.example"))
    assert claims["sub"] == "abc" and claims["email"] == "a@b.example"


def test_identity_from_headers_reads_cognito_claims():
    token = make_jwt(sub="abc", **{"cognito:username": "rick"}, email="rick@example.com")
    ident = identity_from_headers({"Authorization": f"Bearer {token}"}, expected_issuer=ISSUER)
    assert (ident.sub, ident.username, ident.email) == ("abc", "rick", "rick@example.com")
    assert ident.display == "rick@example.com"


def test_username_claim_precedence():
    ident = identity_from_headers({"authorization": f"Bearer {make_jwt(sub='s', username='u1')}"})
    assert ident.username == "u1"
    assert ident.display == "u1"
    assert identity_from_headers({"authorization": f"Bearer {make_jwt(sub='s')}"}).display == "s"


@pytest.mark.parametrize(
    "headers",
    [{}, {"authorization": ""}, {"authorization": "Basic abc"}, {"authorization": "Bearer "}, {"x-other": "1"}],
)
def test_missing_or_non_bearer_header(headers):
    with pytest.raises(AuthError):
        identity_from_headers(headers)


def test_issuer_mismatch():
    token = make_jwt(sub="abc", iss="https://evil.example")
    with pytest.raises(AuthError, match="issuer"):
        identity_from_headers({"authorization": f"Bearer {token}"}, expected_issuer=ISSUER)
    identity_from_headers({"authorization": f"Bearer {token}"})  # no expectation configured: accepted


def test_missing_sub():
    with pytest.raises(AuthError, match="sub"):
        identity_from_headers({"authorization": f"Bearer {make_jwt(email='x@y')}"})


@pytest.mark.parametrize("token", ["not-a-jwt", "a.b", "a.!!!.c", "a." + "e30" + ".c"])
def test_malformed_tokens(token):
    with pytest.raises(AuthError):
        identity_from_headers({"authorization": f"Bearer {token}"})


def test_identity_from_context():
    ident = identity_from_context(make_ctx(make_jwt(sub="ctx-user")), expected_issuer=ISSUER)
    assert ident.sub == "ctx-user"


def test_context_without_request():
    class NoRequest:
        request_context = type("RC", (), {"request": None})()

    with pytest.raises(AuthError):
        identity_from_context(NoRequest())
