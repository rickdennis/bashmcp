"""AWS credentials for the broker.

Three hosting modes, one code path:

* Runlayer Deploy (preferred): Runlayer gives the container an ECS task role and injects
  ``RUNLAYER_DEPLOYMENT_ID`` plus ``RUNLAYER_AWS_ROLE_<NAME>`` for each role declared under
  ``infrastructure.aws.assume_roles``. The app must call sts:AssumeRole itself with
  ExternalId = deployment id. Set ``AWS_ASSUME_ROLE_ARN`` (or let ``RUNLAYER_AWS_ROLE_BASHMCP``
  be picked up) and credentials refresh automatically before they expire.
* EKS with IRSA / AgentCore Runtime: no role to assume; the default credential chain is used.
* Laptop: whatever AWS_PROFILE says.
"""
from __future__ import annotations

import logging
import os

import boto3
from botocore.credentials import AssumeRoleCredentialFetcher, DeferredRefreshableCredentials
from botocore.session import Session as BotocoreSession

log = logging.getLogger("bashmcp.broker.aws")


def assume_role_target() -> tuple[str | None, str | None]:
    """(role_arn, external_id) to assume, or (None, None) to use the ambient credentials."""
    role_arn = os.environ.get("AWS_ASSUME_ROLE_ARN") or os.environ.get("RUNLAYER_AWS_ROLE_BASHMCP") or None
    external_id = os.environ.get("AWS_ASSUME_ROLE_EXTERNAL_ID") or os.environ.get("RUNLAYER_DEPLOYMENT_ID") or None
    return role_arn, external_id


def make_session(region: str, *, role_arn: str | None = None, external_id: str | None = None,
                 session_name: str = "bashmcp-broker") -> boto3.session.Session:
    """A boto3 Session whose credentials come from sts:AssumeRole(role_arn, ExternalId) when a
    role is configured, refreshing themselves in the background; otherwise the default chain."""
    if role_arn is None and external_id is None:
        role_arn, external_id = assume_role_target()
    if not role_arn:
        return boto3.session.Session(region_name=region)

    source = BotocoreSession()
    source.set_config_variable("region", region)
    extra: dict[str, str] = {"RoleSessionName": session_name}
    if external_id:
        extra["ExternalId"] = external_id
    fetcher = AssumeRoleCredentialFetcher(
        client_creator=source.create_client,
        source_credentials=source.get_credentials(),
        role_arn=role_arn,
        extra_args=extra,
    )
    creds = DeferredRefreshableCredentials(refresh_using=fetcher.fetch_credentials, method="assume-role")
    target = BotocoreSession()
    target.set_config_variable("region", region)
    target._credentials = creds  # botocore has no public setter for pre-built refreshable credentials
    log.info("assuming role %s (external id %s) for AgentCore/DynamoDB access", role_arn, "set" if external_id else "none")
    return boto3.session.Session(botocore_session=target, region_name=region)
