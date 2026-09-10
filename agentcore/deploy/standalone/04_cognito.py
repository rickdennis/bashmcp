#!/usr/bin/env python3
"""Step 4: Cognito user pool = the OIDC issuer between Runlayer and the broker runtime.

Creates (idempotently): the pool, a hosted-UI domain, an app client "runlayer" (with secret,
authorization-code + refresh grants; Runlayer's redirect URI goes in --callback-url), an app
client "smoke" (no secret, USER_PASSWORD_AUTH for smoke.py) and one test user. The runlayer
client secret is stored in Secrets Manager and outputs.local.json; the smoke password only in
outputs.local.json (or pass SMOKE_PASSWORD).
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import (  # noqa: E402
    NAMES, TAGS, account_id, banner, base_parser, call, get_output, make_session, set_output,
)

PLACEHOLDER_CALLBACK = "https://srhg.runlayer.com/oauth/callback"
OAUTH_SCOPES = ["openid", "email", "profile"]


def find_pool(cog, name: str) -> str | None:
    token = None
    while True:
        kwargs = {"MaxResults": 60}
        if token:
            kwargs["NextToken"] = token
        resp = cog.list_user_pools(**kwargs)
        for pool in resp.get("UserPools", []):
            if pool["Name"] == name:
                return pool["Id"]
        token = resp.get("NextToken")
        if not token:
            return None


def find_client(cog, pool_id: str, name: str) -> str | None:
    token = None
    while True:
        kwargs = {"UserPoolId": pool_id, "MaxResults": 60}
        if token:
            kwargs["NextToken"] = token
        resp = cog.list_user_pool_clients(**kwargs)
        for client in resp.get("UserPoolClients", []):
            if client["ClientName"] == name:
                return client["ClientId"]
        token = resp.get("NextToken")
        if not token:
            return None


def runlayer_client_spec(pool_id: str, callback_urls: list[str]) -> dict:
    return {
        "UserPoolId": pool_id,
        "ClientName": NAMES["runlayer_client"],
        "GenerateSecret": True,
        "AllowedOAuthFlowsUserPoolClient": True,
        "AllowedOAuthFlows": ["code"],
        "AllowedOAuthScopes": OAUTH_SCOPES,
        "CallbackURLs": callback_urls,
        "SupportedIdentityProviders": ["COGNITO"],
        "ExplicitAuthFlows": ["ALLOW_REFRESH_TOKEN_AUTH"],
        "PreventUserExistenceErrors": "ENABLED",
        "AccessTokenValidity": 60,
        "IdTokenValidity": 60,
        "RefreshTokenValidity": 30,
        "TokenValidityUnits": {"AccessToken": "minutes", "IdToken": "minutes", "RefreshToken": "days"},
        "EnableTokenRevocation": True,
    }


def smoke_client_spec(pool_id: str) -> dict:
    return {
        "UserPoolId": pool_id,
        "ClientName": NAMES["smoke_client"],
        "GenerateSecret": False,
        "ExplicitAuthFlows": ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
        "PreventUserExistenceErrors": "ENABLED",
        "AccessTokenValidity": 60,
        "IdTokenValidity": 60,
        "RefreshTokenValidity": 1,
        "TokenValidityUnits": {"AccessToken": "minutes", "IdToken": "minutes", "RefreshToken": "days"},
        "EnableTokenRevocation": True,
    }


def generate_password() -> str:
    return secrets.token_urlsafe(18) + "Aa1!"


def main() -> None:
    parser = base_parser(__doc__)
    parser.add_argument("--callback-url", action="append", default=[],
                        help="Runlayer OAuth redirect URI (repeatable). Placeholder used until known.")
    parser.add_argument("--smoke-username", default=NAMES["smoke_username"])
    args = parser.parse_args()
    session = make_session(args)
    acct = account_id(session, args.dry_run)
    cog = session.client("cognito-idp")
    sm = session.client("secretsmanager")
    dry = args.dry_run
    banner("04 Cognito user pool, hosted domain, app clients, smoke user")

    callback_urls = args.callback_url or get_output("cognito.callback_urls") or [PLACEHOLDER_CALLBACK]
    domain_prefix = NAMES["cognito_domain_prefix"].format(account_id=acct)

    pool_id = None if dry else find_pool(cog, NAMES["user_pool"])
    if pool_id:
        print(f"user pool {NAMES['user_pool']} exists: {pool_id}")
    else:
        resp = call(cog, "CreateUserPool", {
            "PoolName": NAMES["user_pool"],
            "Policies": {"PasswordPolicy": {
                "MinimumLength": 12, "RequireUppercase": True, "RequireLowercase": True,
                "RequireNumbers": True, "RequireSymbols": True, "TemporaryPasswordValidityDays": 7}},
            "DeletionProtection": "INACTIVE",
            "AutoVerifiedAttributes": ["email"],
            "UsernameAttributes": ["email"],
            "UsernameConfiguration": {"CaseSensitive": False},
            "AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True},
            "MfaConfiguration": "OFF",
            "Schema": [{"Name": "email", "AttributeDataType": "String", "Required": True, "Mutable": True}],
            "UserPoolTags": TAGS,
        }, dry_run=dry)
        pool_id = resp["UserPool"]["Id"] if resp else "us-east-1_DRYRUN0000"

    domain_exists = False
    if not dry:
        desc = cog.describe_user_pool_domain(Domain=domain_prefix).get("DomainDescription") or {}
        domain_exists = bool(desc.get("UserPoolId"))
    if domain_exists:
        print(f"hosted UI domain {domain_prefix} exists")
    else:
        call(cog, "CreateUserPoolDomain", {"Domain": domain_prefix, "UserPoolId": pool_id}, dry_run=dry)

    runlayer_id = None if dry else find_client(cog, pool_id, NAMES["runlayer_client"])
    spec = runlayer_client_spec(pool_id, callback_urls)
    if runlayer_id:
        update = {k: v for k, v in spec.items() if k != "GenerateSecret"}
        update["ClientId"] = runlayer_id
        call(cog, "UpdateUserPoolClient", update, dry_run=dry, label="UpdateUserPoolClient runlayer")
    else:
        resp = call(cog, "CreateUserPoolClient", spec, dry_run=dry, label="CreateUserPoolClient runlayer")
        runlayer_id = resp["UserPoolClient"]["ClientId"] if resp else "DRYRUN-runlayer-client"

    smoke_id = None if dry else find_client(cog, pool_id, NAMES["smoke_client"])
    if smoke_id:
        print(f"smoke client exists: {smoke_id}")
    else:
        resp = call(cog, "CreateUserPoolClient", smoke_client_spec(pool_id), dry_run=dry, label="CreateUserPoolClient smoke")
        smoke_id = resp["UserPoolClient"]["ClientId"] if resp else "DRYRUN-smoke-client"

    password = os.environ.get("SMOKE_PASSWORD") or get_output("cognito.smoke_password", local=True) or generate_password()
    user_exists = False
    if not dry:
        try:
            cog.admin_get_user(UserPoolId=pool_id, Username=args.smoke_username)
            user_exists = True
        except cog.exceptions.UserNotFoundException:
            user_exists = False
    if not user_exists:
        call(cog, "AdminCreateUser", {
            "UserPoolId": pool_id, "Username": args.smoke_username, "MessageAction": "SUPPRESS",
            "UserAttributes": [{"Name": "email", "Value": args.smoke_username}, {"Name": "email_verified", "Value": "true"}],
        }, dry_run=dry)
    call(cog, "AdminSetUserPassword", {
        "UserPoolId": pool_id, "Username": args.smoke_username, "Password": password, "Permanent": True,
    }, dry_run=dry)

    if dry:
        call(sm, "CreateSecret", {"Name": NAMES["secret"], "SecretString": "<runlayer client secret>",
                                  "Tags": [{"Key": k, "Value": v} for k, v in TAGS.items()]}, dry_run=True)
        print("\ndry run complete; nothing written")
        return

    client_secret = cog.describe_user_pool_client(UserPoolId=pool_id, ClientId=runlayer_id)["UserPoolClient"].get("ClientSecret", "")
    try:
        sm.create_secret(Name=NAMES["secret"], SecretString=client_secret,
                         Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()])
    except sm.exceptions.ResourceExistsException:
        sm.put_secret_value(SecretId=NAMES["secret"], SecretString=client_secret)

    issuer = f"https://cognito-idp.{args.region}.amazonaws.com/{pool_id}"
    hosted = f"https://{domain_prefix}.auth.{args.region}.amazoncognito.com"
    set_output("cognito.user_pool_id", pool_id)
    set_output("cognito.issuer", issuer)
    set_output("cognito.discovery_url", f"{issuer}/.well-known/openid-configuration")
    set_output("cognito.hosted_ui_base", hosted)
    set_output("cognito.authorize_url", f"{hosted}/oauth2/authorize")
    set_output("cognito.token_url", f"{hosted}/oauth2/token")
    set_output("cognito.runlayer_client_id", runlayer_id)
    set_output("cognito.smoke_client_id", smoke_id)
    set_output("cognito.smoke_username", args.smoke_username)
    set_output("cognito.callback_urls", callback_urls)
    set_output("cognito.scopes", " ".join(OAUTH_SCOPES))
    set_output("cognito.secret_name", NAMES["secret"])
    set_output("cognito.runlayer_client_secret", client_secret, local=True)
    set_output("cognito.smoke_password", password, local=True)
    print("\nCognito ready; outputs.json and outputs.local.json updated")
    if callback_urls == [PLACEHOLDER_CALLBACK]:
        print("NOTE: callback URL is a placeholder. After creating the Runlayer connector, rerun with "
              "--callback-url <Runlayer redirect URI>.")


if __name__ == "__main__":
    main()
