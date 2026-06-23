#!/usr/bin/env python3
"""Minimal MCP client to drive bash_exec through the router.

  uv run python kind/mcp_smoke.py <url> [num_sessions]

Each session is a fresh MCP connection (fresh Mcp-Session-Id), so the router
places each on a node. Afterwards `kubectl get sessions -A` shows the placements.
Proves the full path: router MCP -> placement -> Session CR -> /exec ->
node-agent -> Firecracker VM.
"""
import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def call_bash(session: ClientSession, command: str) -> str:
    last = None
    # FastMCP may expose the arg nested under "params" or flattened — try both.
    for args in ({"params": {"command": command}}, {"command": command}):
        try:
            res = await session.call_tool("bash_exec", args)
        except Exception as e:  # noqa: BLE001
            last = e
            continue
        if getattr(res, "isError", False):
            last = RuntimeError(f"tool error: {res.content}")
            continue
        return "".join(getattr(b, "text", "") for b in (res.content or [])) or str(res)
    raise last or RuntimeError("call_tool failed")


async def one(url: str, label: str):
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            out = await call_bash(
                session,
                "hostname; ip -4 addr show eth0 2>/dev/null | grep inet | awk '{print $2}'",
            )
            print(f"[{label}]\n{out}\n")


async def main():
    if len(sys.argv) < 2:
        print("usage: mcp_smoke.py <url> [num_sessions]")
        sys.exit(2)
    url = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    for i in range(n):
        await one(url, f"session-{i + 1}")


if __name__ == "__main__":
    asyncio.run(main())
