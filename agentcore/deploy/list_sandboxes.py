#!/usr/bin/env python3
"""Operator view of every sandbox in the registry (all users), with your own AWS credentials.

AgentCore has no API to list runtime sessions, so the DynamoDB registry is the source of truth.
"paused" means the broker stopped it on request; "likely stopped" is inferred from idle time
against the runtime's idle timeout (default 30 min). A stopped sandbox resumes on its next
bash_exec; its /mnt/workspace is kept for 14 idle days.

  uv run deploy/list_sandboxes.py [--profile P] [--region R] [--table bashmcp-sandboxes] [--idle 1800]
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import base_parser, make_session  # noqa: E402


def main() -> None:
    parser = base_parser(__doc__)
    parser.add_argument("--table", default="bashmcp-sandboxes")
    parser.add_argument("--idle", type=int, default=1800, help="runtime idle timeout in seconds (for the inferred state)")
    args = parser.parse_args()
    table = make_session(args).resource("dynamodb").Table(args.table)
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    now = datetime.now(timezone.utc)
    rows = []
    for it in items:
        try:
            last = datetime.fromisoformat(it.get("last_used_at", ""))
            idle = int((now - last).total_seconds())
        except ValueError:
            idle = -1
        state = it.get("status", "?")
        if state != "paused" and idle >= 0:
            state = "likely stopped" if idle > args.idle else "active"
        rows.append((it.get("user_email") or it.get("user_sub", "?"), it["workspace"], state,
                     f"{idle // 60}m" if idle >= 0 else "?", it.get("runtime_session_id", "?"), it.get("created_at", "?")[:19]))
    rows.sort(key=lambda r: (r[0], r[1]))
    hdr = ("USER", "WORKSPACE", "STATE", "IDLE", "RUNTIME_SESSION_ID", "CREATED")
    widths = [max(len(str(r[i])) for r in rows + [hdr]) for i in range(len(hdr))]
    for r in [hdr] + rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(r, widths)))
    print(f"\n{len(rows)} sandbox(es) in {args.table}")


if __name__ == "__main__":
    main()
