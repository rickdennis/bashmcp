from __future__ import annotations

import boto3
import pytest

from broker import awsauth


def test_default_chain_when_no_role(monkeypatch):
    for v in ("AWS_ASSUME_ROLE_ARN", "RUNLAYER_AWS_ROLE_BASHMCP", "AWS_ASSUME_ROLE_EXTERNAL_ID", "RUNLAYER_DEPLOYMENT_ID"):
        monkeypatch.delenv(v, raising=False)
    assert awsauth.assume_role_target() == (None, None)
    s = awsauth.make_session("us-east-1")
    assert isinstance(s, boto3.session.Session) and s.region_name == "us-east-1"


def test_runlayer_injected_env_is_picked_up(monkeypatch):
    monkeypatch.delenv("AWS_ASSUME_ROLE_ARN", raising=False)
    monkeypatch.delenv("AWS_ASSUME_ROLE_EXTERNAL_ID", raising=False)
    monkeypatch.setenv("RUNLAYER_AWS_ROLE_BASHMCP", "arn:aws:iam::558885799623:role/bashmcp-broker-runlayer-nonprod")
    monkeypatch.setenv("RUNLAYER_DEPLOYMENT_ID", "93c0aee8-d082-4a70-81ba-60ab2814d4d2")
    assert awsauth.assume_role_target() == (
        "arn:aws:iam::558885799623:role/bashmcp-broker-runlayer-nonprod",
        "93c0aee8-d082-4a70-81ba-60ab2814d4d2",
    )


def test_explicit_env_overrides_runlayer(monkeypatch):
    monkeypatch.setenv("RUNLAYER_AWS_ROLE_BASHMCP", "arn:aws:iam::1:role/runlayer")
    monkeypatch.setenv("RUNLAYER_DEPLOYMENT_ID", "dep-id")
    monkeypatch.setenv("AWS_ASSUME_ROLE_ARN", "arn:aws:iam::1:role/explicit")
    monkeypatch.setenv("AWS_ASSUME_ROLE_EXTERNAL_ID", "ext")
    assert awsauth.assume_role_target() == ("arn:aws:iam::1:role/explicit", "ext")


def test_assume_role_session_calls_sts_with_external_id(monkeypatch):
    """The refreshable credentials must call sts:AssumeRole with RoleArn + ExternalId on first use."""
    calls: list[dict] = []

    class FakeSts:
        def assume_role(self, **kw):
            calls.append(kw)
            return {"Credentials": {"AccessKeyId": "AKIA", "SecretAccessKey": "s", "SessionToken": "t",
                                    "Expiration": __import__("datetime").datetime(2999, 1, 1, tzinfo=__import__("datetime").timezone.utc)}}

    monkeypatch.setattr(awsauth, "AssumeRoleCredentialFetcher", _fetcher_using(FakeSts()))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "base")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "base")
    s = awsauth.make_session("us-east-1", role_arn="arn:aws:iam::1:role/x", external_id="ext-1")
    creds = s.get_credentials().get_frozen_credentials()
    assert creds.access_key == "AKIA"
    assert calls and calls[0]["RoleArn"] == "arn:aws:iam::1:role/x" and calls[0]["ExternalId"] == "ext-1"
    assert calls[0]["RoleSessionName"] == "bashmcp-broker"


def _fetcher_using(fake_sts):
    from botocore.credentials import AssumeRoleCredentialFetcher

    class Fetcher(AssumeRoleCredentialFetcher):
        def __init__(self, client_creator, source_credentials, role_arn, extra_args=None, **kw):
            super().__init__(lambda *a, **k: fake_sts, source_credentials, role_arn, extra_args=extra_args, **kw)

    return Fetcher
