#!/usr/bin/env python3
"""End-to-end smoke test against the deployed broker (CHECKPOINT C: creates a real microVM session).

Flow: Cognito USER_PASSWORD_AUTH on the "smoke" app client -> MCP streamable HTTP to the broker's
invocation URL with the bearer token -> tools/list -> bash_exec writes a file and prints its uid ->
sandbox_pause -> bash_exec reads the file back (persistence across stop/resume) -> quoting check ->
sandbox_status/list -> optional --destroy.

Usage: uv run smoke.py [--profile P] [--region R] [--workspace smoke] [--destroy] [--keep]
Password comes from SMOKE_PASSWORD or deploy/outputs.local.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import boto3
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

HERE = Path(__file__).resolve().parents[2]
OUTPUTS = json.loads((HERE / "deploy" / "outputs.json").read_text()) if (HERE / "deploy" / "outputs.json").exists() else {}
LOCAL = json.loads((HERE / "deploy" / "outputs.local.json").read_text()) if (HERE / "deploy" / "outputs.local.json").exists() else {}


def get(d: dict, dotted: str, default=None):
    node = d
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def cognito_token(profile: str, region: str) -> str:
    password = os.environ.get("SMOKE_PASSWORD") or get(LOCAL, "cognito.smoke_password")
    if not password:
        sys.exit("no smoke password: set SMOKE_PASSWORD or run deploy/04_cognito.py")
    cog = boto3.session.Session(profile_name=profile, region_name=region).client("cognito-idp")
    resp = cog.initiate_auth(
        ClientId=get(OUTPUTS, "cognito.smoke_client_id"),
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": get(OUTPUTS, "cognito.smoke_username"), "PASSWORD": password},
    )
    return resp["AuthenticationResult"]["AccessToken"]


def text_of(result) -> dict:
    for item in result.content:
        if getattr(item, "type", None) == "text":
            return json.loads(item.text)
    raise AssertionError(f"no text content in {result!r}")


class Check:
    def __init__(self):
        self.failures: list[str] = []

    def ok(self, cond: bool, label: str, detail: str = "") -> None:
        mark = "PASS" if cond else "FAIL"
        print(f"[{mark}] {label}" + (f" -- {detail}" if detail else ""))
        if not cond:
            self.failures.append(label)


async def run(args: argparse.Namespace) -> int:
    url = get(OUTPUTS, "runtimes.broker.invocation_url")
    if not url:
        sys.exit("runtimes.broker.invocation_url missing from deploy/outputs.json; run deploy/06_runtimes.py")
    token = cognito_token(args.profile, args.region)
    headers = {"Authorization": f"Bearer {token}"}
    check = Check()
    ws = args.workspace
    print(f"broker: {url}\nworkspace: {ws}")

    async with streamablehttp_client(url, headers=headers, timeout=timedelta(seconds=120),
                                     sse_read_timeout=timedelta(seconds=900)) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = sorted(t.name for t in (await session.list_tools()).tools)
            check.ok(tools == ["bash_exec", "sandbox_destroy", "sandbox_list", "sandbox_new", "sandbox_pause", "sandbox_status"],
                     "tools/list exposes the six tools", ", ".join(tools))

            t0 = time.monotonic()
            r = text_of(await session.call_tool("bash_exec", {
                "command": "id -u && uname -m && echo hello > /mnt/workspace/smoke.txt && df -h /mnt/workspace | tail -1",
                "workspace": ws, "timeout": 120}))
            cold = time.monotonic() - t0
            check.ok(r.get("returncode") == 0, "first bash_exec succeeds (cold start)", f"{cold:.1f}s, {json.dumps(r)[:300]}")
            check.ok(r.get("stdout", "").startswith("0\n"), "commands run as root (uid 0)", r.get("stdout", "")[:40].strip())

            t0 = time.monotonic()
            r2 = text_of(await session.call_tool("bash_exec", {"command": "echo warm", "workspace": ws}))
            check.ok(r2.get("returncode") == 0 and r2.get("cold_start") is False,
                     "second bash_exec is warm", f"{time.monotonic() - t0:.2f}s")

            r3 = text_of(await session.call_tool("bash_exec", {
                "command": "mkdir -p '/mnt/workspace/a b' && echo ok", "workspace": ws}))
            r4 = text_of(await session.call_tool("bash_exec", {
                "command": "pwd", "working_dir": "/mnt/workspace/a b", "workspace": ws}))
            check.ok(r3.get("returncode") == 0 and r4.get("stdout", "").strip() == "/mnt/workspace/a b",
                     "working_dir with spaces is quoted correctly", r4.get("stdout", "").strip())

            r5 = text_of(await session.call_tool("bash_exec", {
                "command": "cd /nonexistent-dir-xyz", "working_dir": "/nonexistent-dir-xyz", "workspace": ws}))
            check.ok(r5.get("returncode") not in (0, None), "bad working_dir fails loudly", str(r5.get("returncode")))

            st = text_of(await session.call_tool("sandbox_status", {"workspace": ws}))
            check.ok(st.get("status") == "active", "sandbox_status shows active", json.dumps(st)[:200])

            p = text_of(await session.call_tool("sandbox_pause", {"workspace": ws}))
            check.ok(p.get("status") == "paused", "sandbox_pause", p.get("stop_result", ""))
            print("  waiting 20s for StopRuntimeSession to flush storage...")
            await asyncio.sleep(20)

            t0 = time.monotonic()
            r6 = text_of(await session.call_tool("bash_exec", {
                "command": "cat /mnt/workspace/smoke.txt && ls '/mnt/workspace/a b' >/dev/null && echo dir-ok",
                "workspace": ws, "timeout": 120}))
            check.ok(r6.get("returncode") == 0 and r6.get("stdout", "").startswith("hello\n"),
                     "workspace persists across pause/resume", f"{time.monotonic() - t0:.1f}s resume, stdout={r6.get('stdout', '')!r}")

            listing = text_of(await session.call_tool("sandbox_list", {}))
            check.ok(any(s.get("workspace") == ws for s in listing.get("sandboxes", [])), "sandbox_list includes workspace",
                     f"count={listing.get('count')}")

            if args.destroy:
                d = text_of(await session.call_tool("sandbox_destroy", {"workspace": ws}))
                check.ok(d.get("status") == "destroyed", "sandbox_destroy", d.get("stop_result", ""))

    print("\n" + ("ALL CHECKS PASSED" if not check.failures else f"FAILED: {check.failures}"))
    return 0 if not check.failures else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default="aws-sr-am-admins@sr-es-devops-nonprod")
    parser.add_argument("--region", default=get(OUTPUTS, "region", "us-east-1"))
    parser.add_argument("--workspace", default="smoke")
    parser.add_argument("--destroy", action="store_true", help="destroy the smoke workspace at the end")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
