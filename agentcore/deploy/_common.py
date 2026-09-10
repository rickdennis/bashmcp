#!/usr/bin/env python3
"""Shared helpers for the bashmcp AgentCore deploy scripts.

Every script supports --dry-run: it builds the exact boto3 parameters, validates them against
the botocore service model (catching bad names, patterns and ranges offline) and prints them
without calling AWS. Non-secret results go to outputs.json (committed); secrets go to
outputs.local.json (git-ignored).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import boto3
from botocore.validate import validate_parameters

DEFAULT_PROFILE = "aws-sr-am-admins@sr-es-devops-nonprod"
DEFAULT_REGION = "us-east-1"
DEFAULT_ACCOUNT_ID = "558885799623"

NAMES = {
    "sandbox_runtime": "bashmcp_sandbox",   # AgentRuntimeName: letters/digits/underscore only
    "broker_runtime": "bashmcp_broker",
    "sandbox_role": "bashmcp-agentcore-exec-sandbox",
    "broker_role": "bashmcp-agentcore-exec-broker",
    "table": "bashmcp-sandboxes",
    "ecr_sandbox": "bashmcp/sandbox",
    "ecr_broker": "bashmcp/broker",
    "user_pool": "bashmcp-sandbox-users",
    "cognito_domain_prefix": "bashmcp-{account_id}",
    "runlayer_client": "runlayer",
    "smoke_client": "smoke",
    "secret": "bashmcp/cognito/runlayer-client",
    "smoke_username": "bashmcp-smoke@example.com",
}
TAGS = {"Project": "bashmcp", "ManagedBy": "bashmcp/agentcore/deploy", "Team": "code-services"}

HERE = Path(__file__).resolve().parent
OUTPUTS = HERE / "outputs.json"
OUTPUTS_LOCAL = HERE / "outputs.local.json"

SECRET_KEY_RE = re.compile(r"secret|password|token", re.IGNORECASE)


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text() or "{}")


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")


def load_outputs() -> dict[str, Any]:
    return load_json(OUTPUTS)


def save_outputs(data: dict[str, Any]) -> None:
    save_json(OUTPUTS, data)


def load_local() -> dict[str, Any]:
    return load_json(OUTPUTS_LOCAL)


def save_local(data: dict[str, Any]) -> None:
    save_json(OUTPUTS_LOCAL, data)
    OUTPUTS_LOCAL.chmod(0o600)


def _set_nested(data: dict[str, Any], dotted: str, value: Any) -> None:
    node = data
    keys = dotted.split(".")
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = value


def _get_nested(data: dict[str, Any], dotted: str) -> Any:
    node: Any = data
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def set_output(dotted: str, value: Any, *, local: bool = False) -> None:
    data = load_local() if local else load_outputs()
    _set_nested(data, dotted, value)
    (save_local if local else save_outputs)(data)


def get_output(dotted: str, *, local: bool = False, required: bool = False) -> Any:
    value = _get_nested(load_local() if local else load_outputs(), dotted)
    if required and value in (None, "", {}):
        die(f"missing '{dotted}' in {'outputs.local.json' if local else 'outputs.json'}; run the earlier deploy step first")
    return value


def base_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help=f"AWS profile (default {DEFAULT_PROFILE})")
    parser.add_argument("--region", default=DEFAULT_REGION, help=f"AWS region (default {DEFAULT_REGION})")
    parser.add_argument("--dry-run", action="store_true", help="validate and print API calls without executing them")
    parser.add_argument("--yes", action="store_true", help="skip interactive confirmations")
    return parser


def make_session(args: argparse.Namespace) -> boto3.session.Session:
    return boto3.session.Session(profile_name=args.profile, region_name=args.region)


def account_id(session: boto3.session.Session, dry_run: bool) -> str:
    if dry_run:
        return DEFAULT_ACCOUNT_ID
    return session.client("sts").get_caller_identity()["Account"]


def tag_list(extra: dict[str, str] | None = None) -> list[dict[str, str]]:
    tags = {**TAGS, **(extra or {})}
    return [{"Key": k, "Value": v} for k, v in tags.items()]


def snake(op: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", op).lower()


def redact(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if isinstance(value, str) and SECRET_KEY_RE.search(key) and value:
        return "***REDACTED***"
    return value


def call(client: Any, operation: str, params: dict[str, Any], *, dry_run: bool, label: str | None = None) -> Any:
    """Validate params against the botocore model, print them, then call unless dry_run."""
    model = client.meta.service_model.operation_model(operation)
    validate_parameters(params, model.input_shape)
    mode = "DRY RUN" if dry_run else "APPLY"
    print(f"\n== {label or operation} [{client.meta.service_model.service_name}] ({mode}) ==")
    print(json.dumps(redact(params), indent=2, default=str))
    if dry_run:
        return None
    return getattr(client, snake(operation))(**params)


def confirm(message: str, yes: bool) -> bool:
    if yes:
        return True
    answer = input(f"{message} [y/N] ").strip().lower()
    return answer in ("y", "yes")


def die(message: str, code: int = 2) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)


def banner(text: str) -> None:
    print(f"\n{'#' * 78}\n# {text}\n{'#' * 78}")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description="read/write deploy outputs")
    sub = cli.add_subparsers(dest="cmd", required=True)
    p_set = sub.add_parser("set")
    p_set.add_argument("key")
    p_set.add_argument("value")
    p_set.add_argument("--local", action="store_true")
    p_get = sub.add_parser("get")
    p_get.add_argument("key")
    p_get.add_argument("--local", action="store_true")
    ns = cli.parse_args()
    if ns.cmd == "set":
        set_output(ns.key, ns.value, local=ns.local)
    else:
        value = get_output(ns.key, local=ns.local)
        print(value if not isinstance(value, (dict, list)) else json.dumps(value, indent=2))
