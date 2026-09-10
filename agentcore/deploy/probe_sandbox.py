#!/usr/bin/env python3
"""Probe the sandbox runtime directly with YOUR AWS credentials (no Cognito, no broker, no Runlayer).

Uses the same SandboxExecutor code path as the broker, so a pass here means the core mechanism
works: root shell via InvokeAgentRuntimeCommand, /mnt/workspace persistence across
StopRuntimeSession, cold-start timing. Needs runtimes.sandbox.arn in outputs.json (step 06).

  uv run deploy/probe_sandbox.py                      # full cycle: write file -> stop -> read back
  uv run deploy/probe_sandbox.py -c 'uname -a'        # run one command in the probe session
  uv run deploy/probe_sandbox.py --stop               # stop (pause) the probe session
  uv run deploy/probe_sandbox.py --new-session        # start over with a fresh session id

The probe session id is kept in outputs.local.json (probe.session_id) so reruns reuse the same
sandbox, exactly like a broker workspace would.
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from botocore.config import Config  # noqa: E402

from _common import banner, base_parser, get_output, make_session, set_output  # noqa: E402
from broker.executor import SandboxError, SandboxExecutor, build_script  # noqa: E402

WORKDIR = "/mnt/workspace"
HOME = "/mnt/workspace/.home"


def run(executor: SandboxExecutor, sid: str, command: str, timeout: int = 120, label: str | None = None) -> int | None:
    print(f"\n$ {label or command}")
    start = time.monotonic()
    try:
        outcome = executor.run(sid, build_script(command, WORKDIR, HOME), timeout)
    except SandboxError as exc:
        print(f"  ERROR {exc}  ({time.monotonic() - start:.1f}s)")
        return None
    elapsed = time.monotonic() - start
    if outcome.stdout:
        print(outcome.stdout.rstrip("\n"))
    if outcome.stderr:
        print("  [stderr]", outcome.stderr.rstrip("\n"))
    print(f"  -> exit={outcome.exit_code} status={outcome.status} {elapsed:.1f}s"
          f"{' (retried %d times waiting for the microVM)' % (outcome.attempts - 1) if outcome.attempts > 1 else ''}")
    return outcome.exit_code


def main() -> None:
    parser = base_parser(__doc__)
    parser.add_argument("-c", "--command", help="run this single command and exit")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--stop", action="store_true", help="StopRuntimeSession (pause) the probe session and exit")
    parser.add_argument("--new-session", action="store_true", help="forget the stored probe session id first")
    parser.add_argument("--arn", help="sandbox runtime ARN (default: runtimes.sandbox.arn from outputs.json)")
    parser.add_argument("--name", default="bashmcp_sandbox_nonprod",
                        help="sandbox runtime name to resolve via ListAgentRuntimes when no ARN is known")
    args = parser.parse_args()
    if args.dry_run:
        print("probe has no dry-run mode: it only ever talks to an existing sandbox runtime")
        return

    session = make_session(args)
    arn = args.arn or get_output("runtimes.sandbox.arn")
    if not arn:
        control = session.client("bedrock-agentcore-control")
        token = None
        while not arn:
            kwargs = {"maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            resp = control.list_agent_runtimes(**kwargs)
            arn = next((rt["agentRuntimeArn"] for rt in resp.get("agentRuntimes", []) if rt.get("agentRuntimeName") == args.name), None)
            token = resp.get("nextToken")
            if not arn and not token:
                sys.exit(f"no AgentCore runtime named {args.name!r}; pass --arn or --name")
    if args.new_session:
        set_output("probe.session_id", "", local=True)
    sid = get_output("probe.session_id", local=True) or str(uuid.uuid4())
    set_output("probe.session_id", sid, local=True)

    client = session.client("bedrock-agentcore", config=Config(
        retries={"mode": "standard", "max_attempts": 5}, connect_timeout=10, read_timeout=args.timeout + 120))
    executor = SandboxExecutor(client, arn, conflict_max_wait=120)
    banner(f"probe sandbox {arn.rsplit('/', 1)[-1]}  session {sid}")

    if args.stop:
        print("StopRuntimeSession ->", executor.stop(sid))
        return
    if args.command:
        rc = run(executor, sid, args.command, args.timeout)
        sys.exit(0 if rc == 0 else 1)

    failures = 0
    rc = run(executor, sid, "id -u; uname -m; cat /etc/os-release | head -2; df -h /mnt/workspace | tail -1",
             label="who am I, what is this VM (first call cold-starts the microVM)")
    failures += rc != 0
    rc = run(executor, sid, "echo hello-$(date +%s) > /mnt/workspace/probe.txt && cat /mnt/workspace/probe.txt && echo \"$HOME\"")
    failures += rc != 0
    rc = run(executor, sid, "true", label="warm call timing")
    failures += rc != 0
    print("\nStopRuntimeSession (pause) ->", executor.stop(sid))
    print("waiting 20s for storage flush...")
    time.sleep(20)
    rc = run(executor, sid, "cat /mnt/workspace/probe.txt", label="read the file back after stop/resume (cold start again)")
    failures += rc != 0
    rc = run(executor, sid, "test -d /root/.cache && echo 'outside-mount survived?!' || echo 'outside /mnt did not persist (expected)'")
    failures += rc != 0
    print("\n" + ("PROBE PASSED" if not failures else f"PROBE FAILED ({failures} step(s))"))
    print(f"session id kept in outputs.local.json; `--stop` pauses it, `--new-session` starts fresh")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
