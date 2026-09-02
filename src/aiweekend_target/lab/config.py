"""Trusted runtime configuration for the self-contained agent lab.

Model routing is deliberately selected once, when the gateway starts. A chat
request may select only the exact public ``model_id`` from the active profile;
it can never supply an upstream URL, provider, or credential.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from aiweekend_target.errors import ErrorCode, TargetError


MODEL_PAIR = "openai/gpt-oss-20b:groq"
BASE_MODEL = "openai/gpt-oss-20b"
PROVIDER = "groq"
ROUTER_URL = "https://router.huggingface.co/v1"
GATEWAY_BASE_URL = "http://hf-gateway:8080/v1"
MCP_URL = "http://repo-rag:8000/mcp"

DEFAULT_HF_SECRET_PATH = Path("/run/secrets/hf_token")
DEFAULT_LM_STUDIO_URL = "http://host.docker.internal:1234/v1"

ModelBackend = Literal["huggingface", "lmstudio"]
AuthMode = Literal["none", "optional", "required"]


def _configuration_error(message: str) -> TargetError:
    return TargetError(ErrorCode.CONFIG, message)


def _model_id(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > 512:
        raise _configuration_error(f"{field} must be a non-empty exact model identifier")
    if any(ord(character) < 0x20 or character in {"?", "#"} for character in value):
        raise _configuration_error(f"{field} contains characters that are not allowed")
    return value


def _base_url(value: object, backend: ModelBackend) -> str:
    if type(value) is not str or not value or len(value) > 2048:
        raise _configuration_error("ADLC_MODEL_BASE_URL must be an absolute HTTP(S) /v1 URL")
    if backend == "huggingface" and value != ROUTER_URL:
        raise _configuration_error("Hugging Face model routing is pinned to the official Router URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/").split("/")[-1] != "v1"
        or any(segment in {".", ".."} for segment in parsed.path.split("/"))
    ):
        raise _configuration_error(
            "ADLC_MODEL_BASE_URL must be an absolute HTTP(S) /v1 URL without credentials or query"
        )
    normalized_path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))


def _boolean(value: object, field: str, default: bool) -> bool:
    if value is None:
        return default
    if value == "true":
        return True
    if value == "false":
        return False
    raise _configuration_error(f"{field} must be exactly true or false")


@dataclass(frozen=True)
class ModelProfile:
    """A trusted, immutable route to one exact upstream model."""

    backend: ModelBackend
    model_id: str
    base_url: str
    owner: str
    auth_mode: AuthMode
    secret_path: Path | None = None
    discovery_model_id: str | None = None
    provider: str | None = None
    require_tools: bool = True

    def __post_init__(self) -> None:
        if self.backend not in {"huggingface", "lmstudio"}:
            raise _configuration_error("ADLC_MODEL_BACKEND must be huggingface or lmstudio")
        object.__setattr__(self, "model_id", _model_id(self.model_id, "ADLC_MODEL_ID"))
        object.__setattr__(self, "base_url", _base_url(self.base_url, self.backend))
        object.__setattr__(self, "owner", _model_id(self.owner, "model owner"))
        if self.auth_mode not in {"none", "optional", "required"}:
            raise _configuration_error("ADLC_MODEL_AUTH_MODE must be none, optional, or required")
        if self.auth_mode == "required" and self.secret_path is None:
            raise _configuration_error("a required model credential needs ADLC_MODEL_SECRET_PATH")
        if self.auth_mode == "none" and self.secret_path is not None:
            raise _configuration_error("ADLC_MODEL_SECRET_PATH is not allowed when authentication is disabled")
        if (
            self.secret_path is not None
            and not self.secret_path.is_absolute()
            and not PurePosixPath(str(self.secret_path).replace("\\", "/")).is_absolute()
        ):
            raise _configuration_error("ADLC_MODEL_SECRET_PATH must be absolute")

        if self.backend == "huggingface":
            discovery_model_id = _model_id(self.discovery_model_id, "ADLC_HF_BASE_MODEL")
            provider = _model_id(self.provider, "ADLC_HF_PROVIDER")
            if self.base_url != ROUTER_URL:
                raise _configuration_error("Hugging Face model routing is pinned to the official Router URL")
            if self.model_id != f"{discovery_model_id}:{provider}":
                raise _configuration_error("Hugging Face ADLC_MODEL_ID must exactly match base-model:provider")
            if self.auth_mode != "required":
                raise _configuration_error("Hugging Face authentication must be required")
        elif self.discovery_model_id is not None or self.provider is not None:
            raise _configuration_error("provider-specific Hugging Face fields are not allowed for LM Studio")

    @property
    def label(self) -> str:
        return "Hugging Face Router" if self.backend == "huggingface" else "LM Studio"


DEFAULT_MODEL_PROFILE = ModelProfile(
    backend="huggingface",
    model_id=MODEL_PAIR,
    base_url=ROUTER_URL,
    owner=PROVIDER,
    auth_mode="required",
    secret_path=DEFAULT_HF_SECRET_PATH,
    discovery_model_id=BASE_MODEL,
    provider=PROVIDER,
    require_tools=True,
)


def load_model_profile(environ: Mapping[str, str] | None = None) -> ModelProfile:
    """Load one operator-controlled model profile without accepting request data.

    The environment is read only at gateway/runner startup. This is a profile
    selector, not a per-request routing facility.
    """

    values = os.environ if environ is None else environ
    backend_value = values.get("ADLC_MODEL_BACKEND", "huggingface")
    if backend_value not in {"huggingface", "lmstudio"}:
        raise _configuration_error("ADLC_MODEL_BACKEND must be huggingface or lmstudio")
    backend: ModelBackend = backend_value

    require_tools = _boolean(values.get("ADLC_MODEL_REQUIRE_TOOLS"), "ADLC_MODEL_REQUIRE_TOOLS", True)
    if backend == "huggingface":
        base_model = values.get("ADLC_HF_BASE_MODEL", BASE_MODEL)
        provider = values.get("ADLC_HF_PROVIDER", PROVIDER)
        model_id = values.get("ADLC_MODEL_ID", f"{base_model}:{provider}")
        secret_path = Path(values.get("ADLC_MODEL_SECRET_PATH", str(DEFAULT_HF_SECRET_PATH)))
        auth_mode = values.get("ADLC_MODEL_AUTH_MODE", "required")
        return ModelProfile(
            backend="huggingface",
            model_id=model_id,
            base_url=values.get("ADLC_MODEL_BASE_URL", ROUTER_URL),
            owner=provider,
            auth_mode=auth_mode,  # type: ignore[arg-type]
            secret_path=secret_path,
            discovery_model_id=base_model,
            provider=provider,
            require_tools=require_tools,
        )

    model_id = values.get("ADLC_MODEL_ID")
    if model_id is None:
        raise _configuration_error("ADLC_MODEL_ID is required for LM Studio")
    auth_mode = values.get("ADLC_MODEL_AUTH_MODE", "none")
    secret_value = values.get("ADLC_MODEL_SECRET_PATH")
    return ModelProfile(
        backend="lmstudio",
        model_id=model_id,
        base_url=values.get("ADLC_MODEL_BASE_URL", DEFAULT_LM_STUDIO_URL),
        owner=values.get("ADLC_MODEL_OWNER", "lmstudio"),
        auth_mode=auth_mode,  # type: ignore[arg-type]
        secret_path=Path(secret_value) if secret_value else None,
        require_tools=require_tools,
    )


def runtime_model_id(environ: Mapping[str, str] | None = None) -> str:
    """Return the exact model id selected for an agent process.

    The agent receives only this public identifier and the internal gateway
    URL. Provider routing and credentials remain gateway-only configuration.
    """

    values = os.environ if environ is None else environ
    return _model_id(values.get("ADLC_MODEL_ID", MODEL_PAIR), "ADLC_MODEL_ID")


__all__ = [
    "AuthMode",
    "BASE_MODEL",
    "DEFAULT_HF_SECRET_PATH",
    "DEFAULT_LM_STUDIO_URL",
    "DEFAULT_MODEL_PROFILE",
    "GATEWAY_BASE_URL",
    "MCP_URL",
    "MODEL_PAIR",
    "ModelBackend",
    "ModelProfile",
    "PROVIDER",
    "ROUTER_URL",
    "load_model_profile",
    "runtime_model_id",
]
