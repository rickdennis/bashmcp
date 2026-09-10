#!/usr/bin/env python3
"""Step 2: ECR repositories for the two images (idempotent)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import NAMES, account_id, banner, base_parser, call, make_session, set_output, tag_list  # noqa: E402


def ensure_repo(ecr, name: str, dry_run: bool) -> None:
    exists = False
    if not dry_run:
        try:
            ecr.describe_repositories(repositoryNames=[name])
            exists = True
        except ecr.exceptions.RepositoryNotFoundException:
            exists = False
    if exists:
        print(f"repository {name} already exists")
        return
    call(
        ecr,
        "CreateRepository",
        {
            "repositoryName": name,
            "imageScanningConfiguration": {"scanOnPush": True},
            "imageTagMutability": "MUTABLE",
            "tags": tag_list(),
        },
        dry_run=dry_run,
    )


def main() -> None:
    args = base_parser(__doc__).parse_args()
    session = make_session(args)
    acct = account_id(session, args.dry_run)
    ecr = session.client("ecr")
    banner("02 ECR repositories")
    registry = f"{acct}.dkr.ecr.{args.region}.amazonaws.com"
    for key in ("ecr_sandbox", "ecr_broker"):
        ensure_repo(ecr, NAMES[key], args.dry_run)
    if not args.dry_run:
        set_output("ecr.registry", registry)
        set_output("ecr.sandbox_repo_uri", f"{registry}/{NAMES['ecr_sandbox']}")
        set_output("ecr.broker_repo_uri", f"{registry}/{NAMES['ecr_broker']}")
    print(f"\ndocker login: aws ecr get-login-password --profile {args.profile} --region {args.region} "
          f"| docker login --username AWS --password-stdin {registry}")


if __name__ == "__main__":
    main()
