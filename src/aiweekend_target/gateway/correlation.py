"""Bounded transport correlation shared by the gateway client and server."""

from __future__ import annotations

import re


RUN_ID_HEADER = "X-ADLC-Run-ID"
MODEL_REQUEST_ID_HEADER = "X-ADLC-Model-Request-ID"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


def correlation_headers(run_id: str, model_request_id: str) -> dict[str, str]:
    """Return validated internal headers without exposing prompt content."""
    if not isinstance(run_id, str) or not _IDENTIFIER.fullmatch(run_id):
        raise ValueError("transport run id is invalid")
    if not isinstance(model_request_id, str) or not _IDENTIFIER.fullmatch(
        model_request_id
    ):
        raise ValueError("transport model request id is invalid")
    return {
        RUN_ID_HEADER: run_id,
        MODEL_REQUEST_ID_HEADER: model_request_id,
    }


__all__ = [
    "MODEL_REQUEST_ID_HEADER",
    "RUN_ID_HEADER",
    "correlation_headers",
]
