"""Environment-driven settings for the bashmcp AgentCore broker."""
from __future__ import annotations

import os
from dataclasses import dataclass

HARD_MAX_TIMEOUT = 3600  # InvokeAgentRuntimeCommand ceiling
MAX_COMMAND_BYTES = 65536  # InvokeAgentRuntimeCommand ceiling


@dataclass(frozen=True)
class Settings:
    sandbox_arn: str
    table_name: str
    region: str
    max_timeout: int = 600
    default_timeout: int = 60
    default_working_dir: str = "/mnt/workspace"
    home_dir: str = "/mnt/workspace/.home"
    expected_issuer: str | None = None
    idle_timeout: int = 1800
    conflict_max_wait: float = 90.0
    # Identity: "runlayer" verifies Runlayer's Identity Forward token (EdDSA, JWKS);
    # "agentcore-jwt" trusts the Authorization JWT that an AgentCore customJWTAuthorizer verified.
    auth_mode: str = "runlayer"
    runlayer_url: str = "https://stoneridge.runlayer.com"
    runlayer_jwks_url: str | None = None
    runlayer_issuer: str | None = None
    runlayer_audience: str | None = None
    shared_bearer: str | None = None
    sandbox_runtime_name: str | None = None
    workspace_header: str = "x-bashmcp-workspace"


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def load_settings() -> Settings:
    max_timeout = max(1, min(_int("MAX_TIMEOUT", 600), HARD_MAX_TIMEOUT))
    runlayer_url = os.environ.get("RUNLAYER_URL", "https://stoneridge.runlayer.com").rstrip("/")
    auth_mode = os.environ.get("AUTH_MODE", "runlayer").strip().lower()
    if auth_mode not in ("runlayer", "agentcore-jwt"):
        raise RuntimeError(f"AUTH_MODE must be 'runlayer' or 'agentcore-jwt', got {auth_mode!r}")
    return Settings(
        sandbox_arn=os.environ.get("SANDBOX_ARN", ""),
        table_name=os.environ.get("SANDBOX_TABLE", "bashmcp-sandboxes"),
        region=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1",
        max_timeout=max_timeout,
        default_timeout=min(_int("DEFAULT_TIMEOUT", 60), max_timeout),
        default_working_dir=os.environ.get("DEFAULT_WORKING_DIR", "/mnt/workspace"),
        home_dir=os.environ.get("SANDBOX_HOME", "/mnt/workspace/.home"),
        expected_issuer=os.environ.get("EXPECTED_ISSUER") or None,
        idle_timeout=_int("SANDBOX_IDLE_SECONDS", 1800),
        conflict_max_wait=float(os.environ.get("CONFLICT_MAX_WAIT", "90")),
        auth_mode=auth_mode,
        runlayer_url=runlayer_url,
        runlayer_jwks_url=os.environ.get("RUNLAYER_JWKS_URL")
        or f"{runlayer_url}/.well-known/runlayer-identity-forward.jwks.json",
        runlayer_issuer=os.environ.get("RUNLAYER_ISSUER") or runlayer_url,
        runlayer_audience=os.environ.get("RUNLAYER_AUDIENCE") or None,
        shared_bearer=os.environ.get("BROKER_SHARED_BEARER") or None,
        sandbox_runtime_name=os.environ.get("SANDBOX_RUNTIME_NAME") or None,
        workspace_header=os.environ.get("WORKSPACE_HEADER", "x-bashmcp-workspace").lower(),
    )
