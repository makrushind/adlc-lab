"""Domain-neutral contracts shared by batch and live adapters."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeAlias

from aiweekend_target.agent_protocol import strict_json


MAX_BOUNDARY_PAYLOAD_BYTES = 512 * 1024
MAX_COMPONENT_ID_BYTES = 128
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class Boundary(StrEnum):
    INPUT = "input"
    MODEL_REQUEST = "model_request"
    MODEL_OUTPUT = "model_output"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FINAL_OUTPUT = "final_output"


class ControlAction(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    REPLACE = "replace"
    ABORT = "abort"


def detached_json(
    value: object,
    *,
    maximum: int = MAX_BOUNDARY_PAYLOAD_BYTES,
    label: str = "boundary payload",
) -> object:
    """Return an isolated, bounded strict-JSON value."""
    try:
        document = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        if len(document.encode("utf-8")) > maximum:
            raise ValueError(f"{label} exceeds the byte limit")
        return strict_json(document)
    except (TypeError, UnicodeError) as error:
        raise ValueError(f"{label} must be strict JSON") from error


def checked_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} is invalid")
    if len(value.encode("utf-8")) > MAX_COMPONENT_ID_BYTES:
        raise ValueError(f"{label} exceeds the byte limit")
    return value


@dataclass(frozen=True)
class ModelDescriptor:
    """Trusted, reproducible identity and capabilities of one selected model."""

    id: str
    capabilities: frozenset[str] = frozenset()
    provider: str = "unspecified"

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip() or len(self.id.encode()) > 512:
            raise ValueError("model id must be bounded non-empty text")
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("model provider must be non-empty text")
        capabilities = frozenset(
            checked_identifier(item, "model capability") for item in self.capabilities
        )
        object.__setattr__(self, "capabilities", capabilities)


@dataclass(frozen=True)
class ModelInvocation:
    """Host-owned identity for one model transport request."""

    run_id: str
    request_id: str
    user_turn: int
    model_turn: int

    def __post_init__(self) -> None:
        checked_identifier(self.run_id, "run id")
        checked_identifier(self.request_id, "model request id")
        if type(self.user_turn) is not int or self.user_turn < 1:
            raise ValueError("user turn must be positive")
        if type(self.model_turn) is not int or self.model_turn < 1:
            raise ValueError("model turn must be positive")


class ModelProvider(Protocol):
    async def describe(self) -> ModelDescriptor: ...

    async def complete(
        self, request: dict[str, object], invocation: ModelInvocation
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class BoundaryContext:
    """Private value crossing a controlled boundary."""

    boundary: Boundary
    payload: object
    run_id: str
    user_turn: int
    model_turn: int
    correlation_id: str | None = None
    tool_name: str | None = None
    provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.boundary, Boundary):
            raise ValueError("boundary is invalid")
        checked_identifier(self.run_id, "run id")
        if type(self.user_turn) is not int or self.user_turn < 1:
            raise ValueError("user turn must be positive")
        if type(self.model_turn) is not int or self.model_turn < 0:
            raise ValueError("model turn must be non-negative")
        if self.correlation_id is not None:
            checked_identifier(self.correlation_id, "correlation id")
        if self.tool_name is not None and (
            not isinstance(self.tool_name, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.tool_name)
        ):
            raise ValueError("tool name is invalid")
        provenance = tuple(checked_identifier(item, "provenance") for item in self.provenance)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "payload", detached_json(self.payload))

    def with_payload(self, payload: object, *, provenance: Sequence[str] = ()) -> BoundaryContext:
        return BoundaryContext(
            boundary=self.boundary,
            payload=payload,
            run_id=self.run_id,
            user_turn=self.user_turn,
            model_turn=self.model_turn,
            correlation_id=self.correlation_id,
            tool_name=self.tool_name,
            provenance=(*self.provenance, *provenance),
        )


@dataclass(frozen=True)
class Finding:
    """Content-free analyzer output. It cannot itself enforce a decision."""

    analyzer_id: str
    code: str
    severity: str = "medium"

    def __post_init__(self) -> None:
        checked_identifier(self.analyzer_id, "analyzer id")
        checked_identifier(self.code, "finding code")
        if self.severity not in {"info", "low", "medium", "high", "critical"}:
            raise ValueError("finding severity is invalid")


@dataclass(frozen=True)
class TransformOutcome:
    payload: object
    applied: bool
    provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.applied) is not bool:
            raise ValueError("transform applied must be boolean")
        object.__setattr__(self, "payload", detached_json(self.payload))
        object.__setattr__(
            self,
            "provenance",
            tuple(checked_identifier(item, "transform provenance") for item in self.provenance),
        )


@dataclass(frozen=True)
class ControlDecision:
    action: ControlAction
    reason: str
    replacement: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, ControlAction):
            raise ValueError("control action is invalid")
        checked_identifier(self.reason, "control reason")
        if self.action is ControlAction.REPLACE:
            if self.replacement is None:
                raise ValueError("replace decision requires a replacement")
            object.__setattr__(self, "replacement", detached_json(self.replacement))
        elif self.replacement is not None:
            raise ValueError("only replace decisions may contain a replacement")


@dataclass(frozen=True)
class BoundaryEvidence:
    boundary: Boundary
    user_turn: int
    model_turn: int
    correlation_id: str | None
    tool_name: str | None
    original_payload: object
    delivered_payload: object | None
    provenance: tuple[str, ...]
    findings: tuple[Finding, ...]
    decision: ControlDecision

    def __post_init__(self) -> None:
        object.__setattr__(self, "original_payload", detached_json(self.original_payload))
        if self.delivered_payload is not None:
            object.__setattr__(self, "delivered_payload", detached_json(self.delivered_payload))


class Analyzer(Protocol):
    id: str

    def analyze(
        self, context: BoundaryContext
    ) -> Sequence[Finding] | Awaitable[Sequence[Finding]]: ...


class ControlPolicy(Protocol):
    id: str

    def decide(
        self, context: BoundaryContext, findings: Sequence[Finding]
    ) -> ControlDecision | Awaitable[ControlDecision]: ...


class AttackTransform(Protocol):
    id: str

    def transform(
        self, context: BoundaryContext
    ) -> TransformOutcome | Awaitable[TransformOutcome]: ...


class Evaluator(Protocol):
    id: str

    def validate_oracle(self, oracle: object) -> None: ...

    def evaluate(self, evidence: object, oracle: object) -> object: ...


PublicEventSink: TypeAlias = Callable[[dict[str, object]], None]
PrivateEvidenceSink: TypeAlias = Callable[[BoundaryEvidence], None]


__all__ = [
    "Analyzer",
    "AttackTransform",
    "Boundary",
    "BoundaryContext",
    "BoundaryEvidence",
    "ControlAction",
    "ControlDecision",
    "ControlPolicy",
    "Evaluator",
    "Finding",
    "MAX_BOUNDARY_PAYLOAD_BYTES",
    "ModelDescriptor",
    "ModelInvocation",
    "ModelProvider",
    "PrivateEvidenceSink",
    "PublicEventSink",
    "TransformOutcome",
    "checked_identifier",
    "detached_json",
]
