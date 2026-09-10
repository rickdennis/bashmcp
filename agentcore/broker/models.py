"""Wire shapes returned by the broker tools (JSON strings, like server.py's bash_exec)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class ExecResult:
    workspace: str
    runtime_session_id: str
    command: str
    stdout: str
    stderr: str
    returncode: int
    elapsed_seconds: float
    status: str
    cold_start: bool

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def error_json(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra}, indent=2)


def ok_json(**fields: Any) -> str:
    return json.dumps(fields, indent=2, default=str)
