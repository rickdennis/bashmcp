"""Run commands in the sandbox runtime via InvokeAgentRuntimeCommand.

Replaces _ssh_exec in server.py. Each call is a one-shot bash process inside the caller's
microVM (uid 0). The first call on a new or stopped session provisions the microVM, which
can surface as a retryable 409 while AgentCore is still bringing it up.
"""
from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from typing import Any, Callable

from botocore.exceptions import ClientError

RETRYABLE_CODES = {"RetryableConflictException"}
CONFLICT_HINT = "in progress"
BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 10.0)


class SandboxError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class ExecOutcome:
    stdout: str
    stderr: str
    exit_code: int | None
    status: str
    attempts: int = 1
    wait_seconds: float = 0.0


def build_script(command: str, working_dir: str, home_dir: str | None = None) -> str:
    """Wrap the user's command so it runs from working_dir with a HOME inside the mount.

    Everything is passed through shlex.quote, so paths with spaces or quotes are safe.
    `cd ... || exit 1` makes a bad working_dir fail loudly instead of running the command
    somewhere else (server.py concatenated `cd <dir> &&` unquoted).
    """
    lines: list[str] = []
    if home_dir:
        quoted_home = shlex.quote(home_dir)
        lines.append(f"mkdir -p {quoted_home} 2>/dev/null; export HOME={quoted_home}")
    lines.append(f"cd {shlex.quote(working_dir)} || exit 1")
    lines.append(command)
    return f"/bin/bash -lc {shlex.quote(chr(10).join(lines))}"


def _error_parts(exc: ClientError) -> tuple[str, str]:
    error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
    return str(error.get("Code") or type(exc).__name__), str(error.get("Message") or exc)


def _is_retryable(code: str, message: str) -> bool:
    if code in RETRYABLE_CODES:
        return True
    return code == "ConflictException" and CONFLICT_HINT in message.lower()


class SandboxExecutor:
    def __init__(
        self,
        client: Any,
        runtime_arn: str,
        *,
        qualifier: str = "DEFAULT",
        conflict_max_wait: float = 90.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._client = client
        self._arn = runtime_arn
        self._qualifier = qualifier
        self.conflict_max_wait = conflict_max_wait
        self._sleep = sleep

    def run(self, session_id: str, script: str, timeout: int) -> ExecOutcome:
        waited = 0.0
        attempts = 0
        while True:
            attempts += 1
            try:
                outcome = self._invoke(session_id, script, timeout)
            except ClientError as exc:
                code, message = _error_parts(exc)
                if not _is_retryable(code, message) or waited >= self.conflict_max_wait:
                    raise SandboxError(code, message) from exc
                delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
                delay = min(delay, self.conflict_max_wait - waited)
                self._sleep(delay)
                waited += delay
                continue
            outcome.attempts = attempts
            outcome.wait_seconds = waited
            return outcome

    def _invoke(self, session_id: str, script: str, timeout: int) -> ExecOutcome:
        resp = self._client.invoke_agent_runtime_command(
            agentRuntimeArn=self._arn,
            runtimeSessionId=session_id,
            qualifier=self._qualifier,
            contentType="application/json",
            accept="application/vnd.amazon.eventstream",
            body={"command": script, "timeout": int(timeout)},
        )
        stdout: list[str] = []
        stderr: list[str] = []
        exit_code: int | None = None
        status = "UNKNOWN"
        for event in resp.get("stream") or []:
            if not isinstance(event, dict):
                continue
            chunk = event.get("chunk")
            if chunk is None:
                name, payload = next(iter(event.items()))
                message = payload.get("message") if isinstance(payload, dict) else str(payload)
                raise SandboxError(str(name), str(message or name))
            delta = chunk.get("contentDelta")
            if delta:
                stdout.append(delta.get("stdout") or "")
                stderr.append(delta.get("stderr") or "")
            stop = chunk.get("contentStop")
            if stop is not None:
                exit_code = stop.get("exitCode")
                status = stop.get("status") or status
        return ExecOutcome("".join(stdout), "".join(stderr), exit_code, status)

    def stop(self, session_id: str) -> str:
        """StopRuntimeSession == pause. Returns 'stopped', 'already_stopped' or 'stop_in_progress'."""
        try:
            self._client.stop_runtime_session(
                agentRuntimeArn=self._arn,
                runtimeSessionId=session_id,
                qualifier=self._qualifier,
            )
        except ClientError as exc:
            code, message = _error_parts(exc)
            if code == "ResourceNotFoundException":
                return "already_stopped"
            if code in ("ConflictException", "RetryableConflictException"):
                return "stop_in_progress"
            raise SandboxError(code, message) from exc
        return "stopped"
