#!/usr/bin/env python3
"""Step 3: DynamoDB registry table (idempotent)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import NAMES, account_id, banner, base_parser, call, make_session, set_output, tag_list  # noqa: E402


def main() -> None:
    args = base_parser(__doc__).parse_args()
    session = make_session(args)
    acct = account_id(session, args.dry_run)
    ddb = session.client("dynamodb")
    table = NAMES["table"]
    banner("03 DynamoDB table")
    exists = False
    if not args.dry_run:
        try:
            ddb.describe_table(TableName=table)
            exists = True
        except ddb.exceptions.ResourceNotFoundException:
            exists = False
    if exists:
        print(f"table {table} already exists")
    else:
        call(
            ddb,
            "CreateTable",
            {
                "TableName": table,
                "KeySchema": [
                    {"AttributeName": "user_sub", "KeyType": "HASH"},
                    {"AttributeName": "workspace", "KeyType": "RANGE"},
                ],
                "AttributeDefinitions": [
                    {"AttributeName": "user_sub", "AttributeType": "S"},
                    {"AttributeName": "workspace", "AttributeType": "S"},
                ],
                "BillingMode": "PAY_PER_REQUEST",
                "DeletionProtectionEnabled": False,
                "Tags": tag_list(),
            },
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            ddb.get_waiter("table_exists").wait(TableName=table)
    ttl_params = {"TableName": table, "TimeToLiveSpecification": {"Enabled": True, "AttributeName": "expires_at"}}
    if args.dry_run:
        call(ddb, "UpdateTimeToLive", ttl_params, dry_run=True)
    else:
        status = ddb.describe_time_to_live(TableName=table)["TimeToLiveDescription"].get("TimeToLiveStatus")
        if status in ("ENABLED", "ENABLING"):
            print("TTL already enabled")
        else:
            call(ddb, "UpdateTimeToLive", ttl_params, dry_run=False)
        set_output("dynamodb.table_name", table)
        set_output("dynamodb.table_arn", f"arn:aws:dynamodb:{args.region}:{acct}:table/{table}")


if __name__ == "__main__":
    main()
