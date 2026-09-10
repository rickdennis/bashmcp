from __future__ import annotations

import shlex

import pytest

from broker.executor import SandboxError, SandboxExecutor, build_script
from tests.conftest import SANDBOX_ARN, FakeAgentCoreClient, client_error, events

SID = "0f5e1a2b-3c4d-4e5f-8a9b-0c1d2e3f4a5b"


def test_build_script_quotes_working_dir_and_home():
    script = build_script("pwd", "/mnt/workspace/a b", "/mnt/workspace/.home")
    argv = shlex.split(script)
    assert argv[:2] == ["/bin/bash", "-lc"]
    inner = argv[2]
    assert "mkdir -p /mnt/workspace/.home 2>/dev/null; export HOME=/mnt/workspace/.home" in inner
    assert "cd '/mnt/workspace/a b' || exit 1" in inner
    assert inner.endswith("\npwd")


def test_build_script_preserves_command_verbatim():
    command = "echo 'it''s' && printf \"%s\\n\" \"$HOME\"; cat <<'X'\nhi\nX"
    inner = shlex.split(build_script(command, "/mnt/workspace"))[2]
    assert inner.endswith("\n" + command)
    assert "export HOME" not in inner  # no home_dir given


def test_run_collects_stream_and_call_shape():
    client = FakeAgentCoreClient(invoke_results=[events("out\n", "err\n", 3)])
    ex = SandboxExecutor(client, SANDBOX_ARN, sleep=lambda _s: None)
    outcome = ex.run(SID, "the-script", 30)
    assert (outcome.stdout, outcome.stderr, outcome.exit_code, outcome.status) == ("out\n", "err\n", 3, "COMPLETED")
    assert outcome.attempts == 1 and outcome.wait_seconds == 0
    call = client.invoke_calls[0]
    assert call["agentRuntimeArn"] == SANDBOX_ARN
    assert call["runtimeSessionId"] == SID
    assert call["qualifier"] == "DEFAULT"
    assert call["body"] == {"command": "the-script", "timeout": 30}
    assert call["accept"] == "application/vnd.amazon.eventstream"


def test_timed_out_has_no_exit_code():
    client = FakeAgentCoreClient(invoke_results=[events("partial", "", None, "TIMED_OUT")])
    outcome = SandboxExecutor(client, SANDBOX_ARN).run(SID, "sleep 999", 1)
    assert outcome.status == "TIMED_OUT"
    assert outcome.exit_code is None
    assert outcome.stdout == "partial"


def test_inline_error_event_raises():
    client = FakeAgentCoreClient(invoke_results=[[{"validationException": {"message": "bad command"}}]])
    with pytest.raises(SandboxError) as info:
        SandboxExecutor(client, SANDBOX_ARN).run(SID, "x", 5)
    assert info.value.code == "validationException"
    assert "bad command" in info.value.message


def test_retryable_conflict_is_retried():
    sleeps: list[float] = []
    client = FakeAgentCoreClient(invoke_results=[
        client_error("RetryableConflictException", "Session operation in progress, please retry", 409),
        events("ok\n"),
    ])
    ex = SandboxExecutor(client, SANDBOX_ARN, conflict_max_wait=30, sleep=sleeps.append)
    outcome = ex.run(SID, "true", 5)
    assert outcome.stdout == "ok\n"
    assert outcome.attempts == 2
    assert sleeps == [1.0]
    assert outcome.wait_seconds == 1.0


def test_plain_conflict_with_in_progress_message_is_retried():
    client = FakeAgentCoreClient(invoke_results=[
        client_error("ConflictException", "Session operation in progress", 409),
        events("ok"),
    ])
    assert SandboxExecutor(client, SANDBOX_ARN, sleep=lambda _s: None).run(SID, "true", 5).attempts == 2


def test_conflict_gives_up_after_max_wait():
    sleeps: list[float] = []
    err = client_error("RetryableConflictException", "Session operation in progress", 409)
    client = FakeAgentCoreClient(invoke_results=[err] * 10)
    ex = SandboxExecutor(client, SANDBOX_ARN, conflict_max_wait=3, sleep=sleeps.append)
    with pytest.raises(SandboxError) as info:
        ex.run(SID, "true", 5)
    assert info.value.code == "RetryableConflictException"
    assert sum(sleeps) == 3
    assert len(client.invoke_calls) == 3


def test_non_retryable_error_is_raised_immediately():
    client = FakeAgentCoreClient(invoke_results=[client_error("AccessDeniedException", "nope", 403)])
    with pytest.raises(SandboxError) as info:
        SandboxExecutor(client, SANDBOX_ARN).run(SID, "true", 5)
    assert info.value.code == "AccessDeniedException"
    assert len(client.invoke_calls) == 1


def test_stop_results():
    client = FakeAgentCoreClient(stop_results=[
        {},
        client_error("ResourceNotFoundException", "gone", 404, op="StopRuntimeSession"),
        client_error("ConflictException", "Session operation in progress", 409, op="StopRuntimeSession"),
        client_error("AccessDeniedException", "nope", 403, op="StopRuntimeSession"),
    ])
    ex = SandboxExecutor(client, SANDBOX_ARN)
    assert ex.stop(SID) == "stopped"
    assert ex.stop(SID) == "already_stopped"
    assert ex.stop(SID) == "stop_in_progress"
    with pytest.raises(SandboxError):
        ex.stop(SID)
    assert client.stop_calls[0] == {"agentRuntimeArn": SANDBOX_ARN, "runtimeSessionId": SID, "qualifier": "DEFAULT"}
