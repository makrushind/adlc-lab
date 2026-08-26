"""Local configuration, secret, and request policy for the gateway."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aiweekend_target.errors import ErrorCode, TargetError
from aiweekend_target.lab.config import MODEL_PAIR


ROUTING_INJECTION_FIELDS = frozenset(
    {"url", "base_url", "headers", "extra_headers", "api_key", "provider", "providers", "fallback", "fallbacks"}
)


def read_secret(secret_path: str | Path) -> str:
    """Read only the mounted Compose secret, without falling back to the environment."""
    try:
        secret = Path(secret_path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise TargetError(ErrorCode.AUTH, "Hugging Face credential is unavailable") from error
    if not secret:
        raise TargetError(ErrorCode.AUTH, "Hugging Face credential is unavailable")
    return secret


def validate_chat_body(body: Any, selected_model: str) -> Mapping[str, Any]:
    """Reject routing controls and require the exact pinned model before network egress."""
    if not isinstance(body, dict):
        raise TargetError(ErrorCode.POLICY, "request body must be a JSON object")
    if body.get("model") != selected_model:
        raise TargetError(ErrorCode.POLICY, "request model must be the selected exact model:provider pair")
    if ROUTING_INJECTION_FIELDS.intersection(body):
        raise TargetError(ErrorCode.POLICY, "request must not contain routing or credential controls")

    return dict(body)
