#!/usr/bin/env python3
"""Step 6: the two AgentCore Runtimes.

  bashmcp_sandbox  HTTP protocol, IAM inbound, session storage at /mnt/workspace, idle stop 30 min.
  bashmcp_broker   MCP protocol, Cognito JWT inbound, Authorization header forwarded, env wired
                   to the sandbox ARN and the DynamoDB table.

Updating the sandbox runtime creates a new version and WIPES every session's storage, so the
script refuses to update it unless --allow-storage-wipe is given. The broker is safe to update.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import NAMES, TAGS, banner, base_parser, call, die, get_output, make_session, set_output  # noqa: E402

READY_TIMEOUT = 15 * 60


def find_runtime(control, name: str) -> dict | None:
    token = None
    while True:
        kwargs = {"maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = control.list_agent_runtimes(**kwargs)
        for rt in resp.get("agentRuntimes", []):
            if rt.get("agentRuntimeName") == name:
                return rt
        token = resp.get("nextToken")
        if not token:
            return None


def wait_ready(control, runtime_id: str) -> dict:
    deadline = time.time() + READY_TIMEOUT
    while True:
        resp = control.get_agent_runtime(agentRuntimeId=runtime_id)
        status = resp.get("status")
        print(f"  {runtime_id}: {status}")
        if status == "READY":
            return resp
        if status in ("CREATE_FAILED", "UPDATE_FAILED", "DELETING"):
            die(f"runtime {runtime_id} ended in {status}: {resp.get('failureReason', resp)}")
        if time.time() > deadline:
            die(f"runtime {runtime_id} not READY after {READY_TIMEOUT}s")
        time.sleep(10)


def invocation_url(arn: str, region: str) -> str:
    return f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{quote(arn, safe='')}/invocations?qualifier=DEFAULT"


def upsert(control, name: str, create_params: dict, *, dry_run: bool, allow_update: bool, key: str, region: str) -> None:
    existing = None if dry_run else find_runtime(control, name)
    if existing:
        if not allow_update:
            die(f"runtime {name} exists ({existing['agentRuntimeId']}); rerun with the matching --allow-* flag to update")
        update_params = {k: v for k, v in create_params.items() if k not in ("agentRuntimeName", "tags")}
        update_params["agentRuntimeId"] = existing["agentRuntimeId"]
        resp = call(control, "UpdateAgentRuntime", update_params, dry_run=dry_run, label=f"UpdateAgentRuntime {name}")
    else:
        resp = call(control, "CreateAgentRuntime", create_params, dry_run=dry_run, label=f"CreateAgentRuntime {name}")
    if dry_run:
        return
    runtime_id = resp["agentRuntimeId"]
    arn = resp["agentRuntimeArn"]
    wait_ready(control, runtime_id)
    set_output(f"runtimes.{key}.id", runtime_id)
    set_output(f"runtimes.{key}.arn", arn)
    set_output(f"runtimes.{key}.invocation_url", invocation_url(arn, region))
    set_output(f"runtimes.{key}.version", resp.get("agentRuntimeVersion"))


def main() -> None:
    parser = base_parser(__doc__)
    parser.add_argument("--skip-sandbox", action="store_true")
    parser.add_argument("--skip-broker", action="store_true")
    parser.add_argument("--allow-storage-wipe", action="store_true",
                        help="permit UpdateAgentRuntime on the sandbox (resets every session's /mnt/workspace)")
    parser.add_argument("--allow-broker-update", action="store_true", help="permit UpdateAgentRuntime on the broker")
    parser.add_argument("--sandbox-idle", type=int, default=1800, help="sandbox idle stop seconds (60-28800)")
    parser.add_argument("--broker-idle", type=int, default=900, help="broker idle stop seconds (60-28800)")
    parser.add_argument("--max-timeout", type=int, default=600, help="broker bash_exec timeout cap (<=3600)")
    args = parser.parse_args()
    dry = args.dry_run
    session = make_session(args)
    control = session.client("bedrock-agentcore-control")

    if dry:
        acct = get_output("account_id") or "558885799623"
        registry = f"{acct}.dkr.ecr.{args.region}.amazonaws.com"
        images = {
            "sandbox": get_output("images.sandbox") or f"{registry}/{NAMES['ecr_sandbox']}:dryrun",
            "broker": get_output("images.broker") or f"{registry}/{NAMES['ecr_broker']}:dryrun",
        }
        roles = {
            "sandbox": get_output("iam.sandbox_role_arn") or f"arn:aws:iam::{acct}:role/{NAMES['sandbox_role']}",
            "broker": get_output("iam.broker_role_arn") or f"arn:aws:iam::{acct}:role/{NAMES['broker_role']}",
        }
        cognito = {
            "discovery_url": get_output("cognito.discovery_url") or f"https://cognito-idp.{args.region}.amazonaws.com/us-east-1_DRYRUN0000/.well-known/openid-configuration",
            "issuer": get_output("cognito.issuer") or f"https://cognito-idp.{args.region}.amazonaws.com/us-east-1_DRYRUN0000",
            "runlayer_client_id": get_output("cognito.runlayer_client_id") or "dryrun-runlayer",
            "smoke_client_id": get_output("cognito.smoke_client_id") or "dryrun-smoke",
        }
        sandbox_arn = get_output("runtimes.sandbox.arn") or f"arn:aws:bedrock-agentcore:{args.region}:{acct}:runtime/{NAMES['sandbox_runtime']}-dryrun0000"
    else:
        images = {"sandbox": get_output("images.sandbox", required=True), "broker": get_output("images.broker", required=True)}
        roles = {"sandbox": get_output("iam.sandbox_role_arn", required=True), "broker": get_output("iam.broker_role_arn", required=True)}
        cognito = {
            "discovery_url": get_output("cognito.discovery_url", required=True),
            "issuer": get_output("cognito.issuer", required=True),
            "runlayer_client_id": get_output("cognito.runlayer_client_id", required=True),
            "smoke_client_id": get_output("cognito.smoke_client_id", required=True),
        }
        sandbox_arn = get_output("runtimes.sandbox.arn")

    banner("06 AgentCore Runtimes")

    if not args.skip_sandbox:
        sandbox_params = {
            "agentRuntimeName": NAMES["sandbox_runtime"],
            "description": "bashmcp sandbox: per-user root shell microVM with persistent /mnt/workspace",
            "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": images["sandbox"]}},
            "roleArn": roles["sandbox"],
            "networkConfiguration": {"networkMode": "PUBLIC"},
            "protocolConfiguration": {"serverProtocol": "HTTP"},
            "lifecycleConfiguration": {"idleRuntimeSessionTimeout": args.sandbox_idle, "maxLifetime": 28800},
            "filesystemConfigurations": [{"sessionStorage": {"mountPath": "/mnt/workspace"}}],
            "environmentVariables": {"BASHMCP_ROLE": "sandbox"},
            "tags": TAGS,
        }
        upsert(control, NAMES["sandbox_runtime"], sandbox_params, dry_run=dry,
               allow_update=args.allow_storage_wipe, key="sandbox", region=args.region)
        if not dry:
            sandbox_arn = get_output("runtimes.sandbox.arn", required=True)

    if not args.skip_broker:
        if not sandbox_arn:
            die("sandbox runtime ARN unknown; deploy the sandbox first")
        broker_params = {
            "agentRuntimeName": NAMES["broker_runtime"],
            "description": "bashmcp broker: MCP bash_exec front door mapping Cognito users to sandbox sessions",
            "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": images["broker"]}},
            "roleArn": roles["broker"],
            "networkConfiguration": {"networkMode": "PUBLIC"},
            "protocolConfiguration": {"serverProtocol": "MCP"},
            "authorizerConfiguration": {"customJWTAuthorizer": {
                "discoveryUrl": cognito["discovery_url"],
                "allowedClients": [cognito["runlayer_client_id"], cognito["smoke_client_id"]],
            }},
            "requestHeaderConfiguration": {"requestHeaderAllowlist": ["Authorization"]},
            "lifecycleConfiguration": {"idleRuntimeSessionTimeout": args.broker_idle, "maxLifetime": 28800},
            "environmentVariables": {
                "SANDBOX_ARN": sandbox_arn,
                "SANDBOX_TABLE": NAMES["table"],
                "EXPECTED_ISSUER": cognito["issuer"],
                "MAX_TIMEOUT": str(args.max_timeout),
                "SANDBOX_IDLE_SECONDS": str(args.sandbox_idle),
                "LOG_LEVEL": "INFO",
            },
            "tags": TAGS,
        }
        upsert(control, NAMES["broker_runtime"], broker_params, dry_run=dry,
               allow_update=args.allow_broker_update, key="broker", region=args.region)

    if dry:
        print("\ndry run complete; nothing created")
        return

    url = get_output("runtimes.broker.invocation_url")
    print("\nRunlayer connector settings:")
    print(f"  transport      streaming-http")
    print(f"  url            {url}")
    print(f"  auth           manual OAuth 2.1 (disable auto-detect)")
    print(f"  authorize URL  {get_output('cognito.authorize_url')}")
    print(f"  token URL      {get_output('cognito.token_url')}")
    print(f"  client id      {cognito['runlayer_client_id']}   (secret: Secrets Manager {NAMES['secret']})")
    print(f"  scopes         {get_output('cognito.scopes')}")


if __name__ == "__main__":
    main()
