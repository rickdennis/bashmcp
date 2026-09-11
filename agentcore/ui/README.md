# bashmcp sandbox operator UI (local)

A small web page for operating the AgentCore sandboxes behind the bashmcp broker: see every
user's sandbox from the DynamoDB registry, the runtime's status, a feed of recent commands from
the runtime's CloudWatch log group, and run a command in (or pause) a selected sandbox.

It runs on **your laptop, bound to 127.0.0.1**, with **your own AWS credentials**. Every
`InvokeAgentRuntimeCommand` / `StopRuntimeSession` it makes is recorded in CloudTrail under your
identity. Commands run as **root inside another user's microVM**; the page says so in a banner.

## Run

From the `agentcore/` directory (it is the uv project):

```bash
uv run ui/app.py --profile aws-sr-am-admins@sr-es-devops-nonprod
# or
uv run python -m ui.app --profile aws-sr-am-admins@sr-es-devops-nonprod
```

Then open <http://127.0.0.1:8787>.

| Flag | Default | Meaning |
|------|---------|---------|
| `--profile` | `AWS_PROFILE` / default chain | AWS named profile (the deploy scripts use `aws-sr-am-admins@sr-es-devops-nonprod`) |
| `--region` | `us-east-1` | |
| `--port` | `8787` | loopback only; there is no flag to bind elsewhere on purpose |
| `--table` | `bashmcp-sandboxes` | registry table |
| `--runtime-name` | `bashmcp_sandbox_nonprod` | sandbox AgentCore runtime; the ARN is resolved with `ListAgentRuntimes` at startup |
| `--idle` | `1800` | runtime idle timeout used to infer "likely stopped" |

IAM needed: `dynamodb:Scan` + `dynamodb:UpdateItem` on the table, `bedrock-agentcore:ListAgentRuntimes`,
`GetAgentRuntime`, `InvokeAgentRuntimeCommand`, `StopRuntimeSession`, `logs:FilterLogEvents` on
`/aws/bedrock-agentcore/runtimes/<runtimeId>-DEFAULT`, and `sts:GetCallerIdentity` (informational).

## What the page shows

* **Runtime card**: name, id, status, version, ARN, region/profile, the identity you are acting as,
  and the log group used for the activity feed.
* **Sandboxes**: one row per registry item (all users). State is inferred like
  `deploy/list_sandboxes.py`: `paused` if the broker paused it, else `likely stopped` when idle
  longer than `--idle`, else `active`. Text filter; auto-refresh every 15 s.
* **Selected sandbox**: command box, `working_dir` (default `/mnt/workspace`), timeout (1-600 s),
  Run, Pause, two quick actions. Output shows stdout and stderr (stderr tinted), then exit code,
  status, elapsed and attempts. While a command runs the server sends a heartbeat every 2 s so a
  cold microVM start (10-60 s) is visibly "running", not hung.
* **Recent commands**: `command=` lines from the runtime log group over the last N minutes,
  newest first (cap 100), with the `build_script` wrapper stripped so you see the command as the
  user typed it.

## API

| Method | Path | Body / query | Returns |
|--------|------|--------------|---------|
| GET | `/api/runtime` | | `{name,id,arn,status,version,last_updated,region,profile,log_group,caller_arn,...}` |
| GET | `/api/sandboxes` | | `{sandboxes:[...], count, table, idle_seconds}` |
| GET | `/api/activity` | `?minutes=60&limit=50` | `{events:[{time,command,request_id,working_dir,log_stream}], log_group}` |
| POST | `/api/exec` | `{runtime_session_id, command, working_dir?, timeout?}` | `text/event-stream`: `start`, `heartbeat`*, `stdout`, `stderr`, `done{exit_code,status,elapsed_seconds,attempts}` or `error{code,message}` |
| POST | `/api/pause` | `{runtime_session_id}` | `{result: stopped\|already_stopped\|stop_in_progress, ...}`; the row's status is set to `paused` |

`runtime_session_id` must match a row in the registry; arbitrary ids are refused (404). POSTs
must carry the `x-bashmcp-ui: 1` header (the page sets it) and every request must arrive with a
loopback `Host` - both are cheap guards against a web page in your browser driving this server
cross-site or via DNS rebinding.

## Notes and limits

* AgentCore's `InvokeAgentRuntimeCommand` response is collected by `SandboxExecutor.run` before it
  returns, so stdout/stderr arrive at the end of a command, after the heartbeats. True incremental
  output needs the executor to yield deltas; the SSE protocol here already has per-chunk events
  for that day.
* There is no cancel: a running command continues in the microVM until it exits or hits its
  timeout, even if you close the tab.
* An exec through the UI touches the row (`status=active`, `last_used_at=now`) because the
  microVM is running again; a pause sets `status=paused`. Nothing else in the registry is written.
* The log-group name is derived as `/aws/bedrock-agentcore/runtimes/<runtimeId>-DEFAULT`; only
  the DEFAULT endpoint is used by this project. If the group does not exist the feed is empty
  with an explanatory error, not a failure.
* Command text is never written to disk by this app; uvicorn's access log prints method + path only.

## Tests

```bash
uv run pytest -q tests/test_ui.py
```

Fakes: moto DynamoDB (`ddb_table`), `FakeAgentCoreClient` (exec/stop), plus in-file fakes for
`bedrock-agentcore-control` and CloudWatch Logs.
