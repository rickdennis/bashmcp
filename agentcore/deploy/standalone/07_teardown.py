#!/usr/bin/env python3
"""Step 7 (optional): delete everything the other scripts created, newest first."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import NAMES, banner, base_parser, confirm, get_output, load_outputs, make_session, save_outputs  # noqa: E402


def main() -> None:
    args = base_parser(__doc__).parse_args()
    session = make_session(args)
    outputs = load_outputs()
    banner("07 teardown")
    if args.dry_run:
        print("would delete:", {k: v for k, v in outputs.items() if k in ("runtimes", "dynamodb", "cognito", "ecr", "iam")})
        return

    control = session.client("bedrock-agentcore-control")
    for key in ("broker", "sandbox"):
        rid = get_output(f"runtimes.{key}.id")
        if rid and confirm(f"delete runtime {rid}?", args.yes):
            control.delete_agent_runtime(agentRuntimeId=rid)
            outputs.get("runtimes", {}).pop(key, None)
            save_outputs(outputs)

    table = get_output("dynamodb.table_name")
    if table and confirm(f"delete DynamoDB table {table} (all sandbox registrations)?", args.yes):
        session.client("dynamodb").delete_table(TableName=table)
        outputs.pop("dynamodb", None)
        save_outputs(outputs)

    pool_id = get_output("cognito.user_pool_id")
    if pool_id and confirm(f"delete Cognito pool {pool_id}, its domain and the Runlayer client secret?", args.yes):
        cog = session.client("cognito-idp")
        domain = get_output("cognito.hosted_ui_base", ) or ""
        prefix = domain.replace("https://", "").split(".")[0] if domain else None
        if prefix:
            try:
                cog.delete_user_pool_domain(Domain=prefix, UserPoolId=pool_id)
            except cog.exceptions.InvalidParameterException:
                pass
        cog.delete_user_pool(UserPoolId=pool_id)
        try:
            session.client("secretsmanager").delete_secret(SecretId=NAMES["secret"], ForceDeleteWithoutRecovery=True)
        except session.client("secretsmanager").exceptions.ResourceNotFoundException:
            pass
        outputs.pop("cognito", None)
        save_outputs(outputs)

    if get_output("ecr.registry") and confirm("delete ECR repositories (and all images)?", args.yes):
        ecr = session.client("ecr")
        for key in ("ecr_sandbox", "ecr_broker"):
            try:
                ecr.delete_repository(repositoryName=NAMES[key], force=True)
            except ecr.exceptions.RepositoryNotFoundException:
                pass
        outputs.pop("ecr", None)
        outputs.pop("images", None)
        save_outputs(outputs)

    if get_output("iam.broker_role_arn") and confirm("delete IAM execution roles?", args.yes):
        iam = session.client("iam")
        for role in (NAMES["broker_role"], NAMES["sandbox_role"]):
            try:
                for pol in iam.list_role_policies(RoleName=role)["PolicyNames"]:
                    iam.delete_role_policy(RoleName=role, PolicyName=pol)
                iam.delete_role(RoleName=role)
            except iam.exceptions.NoSuchEntityException:
                pass
        outputs.pop("iam", None)
        save_outputs(outputs)
    print("teardown complete")


if __name__ == "__main__":
    main()
