#!/usr/bin/env python3
"""Step 1: execution roles for the sandbox and broker runtimes (idempotent)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import NAMES, account_id, banner, base_parser, call, make_session, set_output, tag_list  # noqa: E402

IAM_DIR = Path(__file__).resolve().parent / "iam"


def render(name: str, ctx: dict[str, str]) -> str:
    text = (IAM_DIR / name).read_text()
    for key, value in ctx.items():
        text = text.replace("{{" + key + "}}", value)
    json.loads(text)  # fail fast on bad JSON
    return text


def ensure_role(iam, role_name: str, trust: str, policy_name: str, policy: str, dry_run: bool) -> None:
    exists = False
    if not dry_run:
        try:
            iam.get_role(RoleName=role_name)
            exists = True
        except iam.exceptions.NoSuchEntityException:
            exists = False
    if exists:
        call(iam, "UpdateAssumeRolePolicy", {"RoleName": role_name, "PolicyDocument": trust}, dry_run=dry_run)
    else:
        call(
            iam,
            "CreateRole",
            {
                "RoleName": role_name,
                "AssumeRolePolicyDocument": trust,
                "Description": "bashmcp AgentCore Runtime execution role",
                "Tags": tag_list(),
            },
            dry_run=dry_run,
        )
    call(
        iam,
        "PutRolePolicy",
        {"RoleName": role_name, "PolicyName": policy_name, "PolicyDocument": policy},
        dry_run=dry_run,
    )


def main() -> None:
    args = base_parser(__doc__).parse_args()
    session = make_session(args)
    acct = account_id(session, args.dry_run)
    iam = session.client("iam")
    ctx = {
        "ACCOUNT_ID": acct,
        "REGION": args.region,
        "SANDBOX_RUNTIME_NAME": NAMES["sandbox_runtime"],
        "BROKER_RUNTIME_NAME": NAMES["broker_runtime"],
        "TABLE_NAME": NAMES["table"],
        "ECR_SANDBOX": NAMES["ecr_sandbox"],
        "ECR_BROKER": NAMES["ecr_broker"],
    }
    banner("01 IAM execution roles")
    ensure_role(iam, NAMES["sandbox_role"], render("sandbox-trust.json", ctx), "bashmcp-sandbox-exec",
                render("sandbox-policy.json", ctx), args.dry_run)
    ensure_role(iam, NAMES["broker_role"], render("broker-trust.json", ctx), "bashmcp-broker-exec",
                render("broker-policy.json", ctx), args.dry_run)
    if not args.dry_run:
        set_output("account_id", acct)
        set_output("region", args.region)
        set_output("iam.sandbox_role_arn", f"arn:aws:iam::{acct}:role/{NAMES['sandbox_role']}")
        set_output("iam.broker_role_arn", f"arn:aws:iam::{acct}:role/{NAMES['broker_role']}")
        print("\nroles ready; outputs.json updated")


if __name__ == "__main__":
    main()
