"""Stable structured errors returned by target commands."""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    CONFIG = "CONFIG"
    BUILD = "BUILD"
    RESOURCE = "RESOURCE"
    AUTH = "AUTH"
    QUOTA = "QUOTA"
    PROVIDER = "PROVIDER"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MCP = "MCP"
    POLICY = "POLICY"
    TEST = "TEST"
    BUSY = "BUSY"


_LOCAL_RESPONSE_STATUSES = {
    ErrorCode.AUTH: 401,
    ErrorCode.QUOTA: 402,
    ErrorCode.MODEL_UNAVAILABLE: 404,
    ErrorCode.PROVIDER: 400,
    ErrorCode.POLICY: 400,
    ErrorCode.CONFIG: 400,
}


def classify_upstream_status(status_code: int) -> ErrorCode:
    """Classify an upstream HTTP status without changing local response semantics."""
    if status_code in {401, 403}:
        return ErrorCode.AUTH
    if status_code in {402, 429}:
        return ErrorCode.QUOTA
    if status_code == 404:
        return ErrorCode.MODEL_UNAVAILABLE
    return ErrorCode.PROVIDER


def local_response_status(code: ErrorCode) -> int:
    """Return the stable HTTP status exposed by the local gateway for an error code."""
    return _LOCAL_RESPONSE_STATUSES.get(code, 500)


class TargetError(Exception):
    """An error with a machine-readable stable code and process status."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: Any = None,
        exit_code: int = 1,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.exit_code = exit_code

    def as_result(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code.value,
                "message": self.message,
                "details": _json_safe(self.details),
            },
            "exit_code": self.exit_code,
        }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, set | frozenset):
        items = [_json_safe(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    return str(value)
