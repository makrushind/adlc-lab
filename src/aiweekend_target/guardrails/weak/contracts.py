"""Bounded runtime contracts for the optional, intentionally incomplete guardrail."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

POLICY_VERSION = "adlc-guardrail-1"
MAX_EVENT_BYTES = 64 * 1024
BOUNDARIES = frozenset({"input", "tool_call", "tool_result", "final_output"})
REVIEW_REASONS = {
    "ALLOW": frozenset(
        {"ORDINARY_SUPPORT", "POLICY_QUESTION", "APPEAL_DECISION", "ACCOUNT_SAFETY"}
    ),
    "ALLOW_AS_DATA": frozenset({"QUOTED_ABUSE", "QUOTED_SCAM", "QUOTED_INJECTION"}),
    "BLOCK": frozenset(
        {
            "GENERATE_ABUSE",
            "MODERATION_EVASION",
            "PROMPT_OVERRIDE",
            "PRIVATE_DATA_REQUEST",
            "UNAUTHORIZED_ACTION",
        }
    ),
    "ESCALATE": frozenset({"IMMINENT_SAFETY_RISK"}),
}


def identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,95}", value) is not None
    )


def _tool_identifier(value: object) -> bool:
    """Use the same tool-name grammar as the host ToolSpec/BoundaryContext."""
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is not None
    )


def freeze(value: object) -> object:
    """Detach JSON recursively; no mutable aliases cross the guardrail boundary."""
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON keys must be strings")
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("value must be finite JSON")


def thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


def encoded(value: object) -> bytes:
    return json.dumps(
        thaw(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def payload_size(value: object) -> int:
    try:
        return (
            len(value.encode("utf-8"))
            if isinstance(value, str)
            else len(encoded(value))
        )
    except UnicodeError as error:
        raise ValueError("payload must be valid UTF-8") from error


@dataclass(frozen=True)
class Source:
    source_id: str
    kind: str
    text: str

    def __post_init__(self) -> None:
        if not identifier(self.source_id) or self.kind not in {
            "active",
            "evidence",
            "field",
            "output",
        }:
            raise ValueError("invalid source identity")
        if not isinstance(self.text, str):
            raise TypeError("source text must be a string")


@dataclass(frozen=True)
class GuardrailEvent:
    boundary: str
    payload: object
    sources: tuple[Source, ...] = ()
    tool_name: str | None = None

    def __post_init__(self) -> None:
        if self.boundary not in BOUNDARIES:
            raise ValueError("unsupported guardrail boundary")
        if self.tool_name is not None and not _tool_identifier(self.tool_name):
            raise ValueError("invalid tool identity")
        payload = freeze(self.payload)
        if payload_size(payload) > MAX_EVENT_BYTES:
            raise ValueError("event exceeds 64 KiB")
        if any(not isinstance(item, Source) for item in self.sources):
            raise ValueError("invalid source")
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "sources", tuple(self.sources))


@dataclass(frozen=True)
class GuardrailRunContext:
    grants: tuple[Mapping[str, object], ...] = ()
    protected_values: tuple[str, ...] = ()
    forbidden_sinks: tuple[str, ...] = ("model", "external_tool", "user")
    result_contracts: Mapping[str, object] = field(default_factory=dict)
    detector_requirements: Mapping[str, object] = field(default_factory=dict)
    visible_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.grants, (list, tuple)) or any(
            not isinstance(grant, Mapping) for grant in self.grants
        ):
            raise TypeError("grants must contain objects")
        for grant in self.grants:
            if not _tool_identifier(grant.get("tool")):
                raise ValueError("grant tool is invalid")
            if set(grant) == {"tool", "arguments"}:
                if not isinstance(grant["arguments"], Mapping):
                    raise TypeError("grant arguments must be an object")
            elif set(grant) == {"tool", "argument_rule"}:
                if (
                    grant["argument_rule"]
                    != "any_schema_valid_record_without_protected_values"
                ):
                    raise ValueError("unknown grant rule")
            else:
                raise ValueError(
                    "grant must specify exact arguments or a published argument rule"
                )
        for name in ("result_contracts", "detector_requirements"):
            if not isinstance(getattr(self, name), Mapping):
                raise TypeError(f"{name} must be an object")
        for name in ("grants", "result_contracts", "detector_requirements"):
            object.__setattr__(self, name, freeze(getattr(self, name)))
        for name in ("protected_values", "forbidden_sinks", "visible_tools"):
            values = tuple(getattr(self, name))
            if any(not isinstance(item, str) or not item for item in values):
                raise ValueError(f"invalid {name}")
            object.__setattr__(self, name, values)


@dataclass(frozen=True)
class Signal:
    code: str
    source_id: str
    detector: str
    attack_similarity: float | None = None
    benign_similarity: float | None = None

    def __post_init__(self) -> None:
        for item in (self.code, self.source_id, self.detector):
            if not identifier(item):
                raise ValueError("signal identifiers must be content-free codes")
        for value in (self.attack_similarity, self.benign_similarity):
            if value is not None and (
                not isinstance(value, (int, float)) or not math.isfinite(value)
            ):
                raise ValueError("invalid similarity")


@dataclass(frozen=True)
class GuardrailResult:
    action: str
    reason: str
    signals: tuple[Signal, ...] = ()
    replacement: object | None = None
    degraded: bool = False
    attention: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.action not in {"allow", "block", "replace", "abort"}:
            raise ValueError("invalid action")
        if not identifier(self.reason):
            raise ValueError("invalid reason code")
        if type(self.degraded) is not bool or any(
            not identifier(item) for item in self.attention
        ):
            raise ValueError("invalid result diagnostics")
        if any(not isinstance(item, Signal) for item in self.signals):
            raise ValueError("invalid signals")
        if (self.action == "replace") != (self.replacement is not None):
            raise ValueError("replacement must accompany replace only")
        if self.replacement is not None:
            value = freeze(self.replacement)
            if payload_size(value) > MAX_EVENT_BYTES:
                raise ValueError("replacement exceeds 64 KiB")
            object.__setattr__(self, "replacement", value)
        object.__setattr__(self, "signals", tuple(self.signals))
        object.__setattr__(self, "attention", tuple(self.attention))


@dataclass(frozen=True)
class ReviewRequest:
    message: str
    evidence: tuple[str, ...] = ()
    context: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.message, str) or any(
            not isinstance(item, str) for item in self.evidence
        ):
            raise TypeError("review message and evidence must be text")
        if not isinstance(self.context, Mapping):
            raise TypeError("review context must be an object")
        defaults = {
            "route": "general",
            "actor_role": "end_user",
            "target_relation": "self",
            "requested_operation": "none",
            "allowed_operations": ["none"],
        }
        defaults.update(self.context)
        object.__setattr__(self, "context", freeze(defaults))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if (
            len(
                encoded(
                    {
                        "message": self.message,
                        "evidence": self.evidence,
                        "context": defaults,
                    }
                )
            )
            > MAX_EVENT_BYTES
        ):
            raise ValueError("review request exceeds 64 KiB")


@dataclass(frozen=True)
class ReviewResult:
    action: str
    reason_code: str
    signals: tuple[Signal, ...] = ()
    attention: tuple[str, ...] = ()
    degraded: bool = False
    policy_version: str = POLICY_VERSION

    def __post_init__(self) -> None:
        if (
            self.action not in REVIEW_REASONS
            or self.reason_code not in REVIEW_REASONS[self.action]
        ):
            raise ValueError("invalid review action/reason pair")
        if any(not isinstance(item, Signal) for item in self.signals):
            raise ValueError("invalid review signals")
        if type(self.degraded) is not bool or any(
            not identifier(item) for item in self.attention
        ):
            raise ValueError("invalid review diagnostics")
        if self.policy_version != POLICY_VERSION:
            raise ValueError("unsupported review policy version")
        object.__setattr__(self, "signals", tuple(self.signals))
        object.__setattr__(self, "attention", tuple(self.attention))


def validate_diagnostics(
    result: GuardrailResult | ReviewResult, protected_values: tuple[str, ...]
) -> None:
    """Identifiers are not a channel for protected content, even if a detector misbehaves."""
    metadata = [
        getattr(result, "reason", getattr(result, "reason_code", "")),
        *result.attention,
    ]
    for signal in result.signals:
        metadata.extend((signal.code, signal.source_id, signal.detector))
    if any(secret in value for secret in protected_values for value in metadata):
        raise ValueError("guardrail diagnostics contain protected data")
