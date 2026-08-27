"""Stable structured errors returned by target commands."""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    CONFIG = "CONFIG"
    AUTH = "AUTH"
    QUOTA = "QUOTA"
    PROVIDER = "PROVIDER"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MCP = "MCP"
    POLICY = "POLICY"


_LOCAL_RESPONSE_STATUSES = {
    ErrorCode.AUTH: 401,
    ErrorCode.QUOTA: 402,
    ErrorCode.MODEL_UNAVAILABLE: 404,
    ErrorCode.PROVIDER: 400,
    ErrorCode.POLICY: 400,
    ErrorCode.CONFIG: 400,
}
_GATEWAY_CHAT_ERROR_STATUSES = {
    ErrorCode.AUTH: 401,
    ErrorCode.QUOTA: 402,
    ErrorCode.MODEL_UNAVAILABLE: 404,
    ErrorCode.PROVIDER: 400,
    ErrorCode.POLICY: 400,
}
_GATEWAY_READINESS_ERROR_STATUSES = {
    ErrorCode.AUTH: 401,
    ErrorCode.QUOTA: 402,
    ErrorCode.MODEL_UNAVAILABLE: 404,
    ErrorCode.PROVIDER: 400,
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


def match_gateway_error(document: object, status_code: object, *, readiness: bool = False) -> ErrorCode | None:
    """Return a canonical peer-gateway code only for an exact allowed document."""
    if type(status_code) is not int or not isinstance(document, dict) or set(document) != {"ok", "error", "exit_code"}:
        return None
    error = document.get("error")
    if (
        document.get("ok") is not False
        or type(document.get("exit_code")) is not int
        or document["exit_code"] != 1
        or not isinstance(error, dict)
        or set(error) != {"code", "message", "details"}
        or not isinstance(error.get("code"), str)
        or not isinstance(error.get("message"), str)
        or (error.get("details") is not None and not isinstance(error.get("details"), dict))
    ):
        return None
    allowed = _GATEWAY_READINESS_ERROR_STATUSES if readiness else _GATEWAY_CHAT_ERROR_STATUSES
    for code, expected_status in allowed.items():
        if error["code"] == code.value and status_code == expected_status:
            return code
    return None


class TargetError(Exception):
    """An error with a machine-readable stable code and process status."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: Any = None,
        exit_code: int = 1,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.exit_code = exit_code
        self.diagnostics = dict(diagnostics) if diagnostics is not None else None

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
