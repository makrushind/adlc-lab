"""Provider-neutral, bounded trace events for autonomous lab runs.

The v2 trace deliberately stores normalized facts rather than raw prompts, tool
results, or credentials.  A runner may keep richer evidence in memory for the
private evaluator, but that evidence must not be copied into this public trace.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType


MAX_EVENT_BYTES = 65_536
MAX_FACT_DEPTH = 8
MAX_COLLECTION_ITEMS = 256
MAX_STRING_BYTES = 16_384
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CREDENTIAL = re.compile(
    r"(?i)\b(?:authorization|proxy-authorization|x-api-key|api-key|cookie|set-cookie|"
    r"x-auth-token|token|secret|password)\s*[:=]\s*[^\r\n]+|\bbearer\s+[^\s,;]+|"
    r"\bhf_[A-Za-z0-9_-]+"
)


class Taint(StrEnum):
    """Stable data-origin labels understood by scenarios, policy, and reports."""

    PROMPT_UNTRUSTED = "PROMPT_UNTRUSTED"
    RAG_UNTRUSTED = "RAG_UNTRUSTED"
    MCP_UNTRUSTED = "MCP_UNTRUSTED"
    TOOL_DESCRIPTION_UNTRUSTED = "TOOL_DESCRIPTION_UNTRUSTED"
    MODEL_OUTPUT_UNTRUSTED = "MODEL_OUTPUT_UNTRUSTED"
    SECRET = "SECRET"
    USER_DATA = "USER_DATA"


class TraceValidationError(ValueError):
    """A caller tried to put an invalid or unbounded fact into the trace."""


@dataclass(frozen=True)
class TraceEvent:
    """One normalized event in a v2 run trace."""

    run_id: str
    sequence: int
    timestamp: str
    event_type: str
    scenario_id: str
    model_profile: str
    turn: int | None
    correlation_id: str | None
    taints: tuple[str, ...]
    redactions: tuple[str, ...]
    facts: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": 2,
            "run_id": self.run_id,
            "seq": self.sequence,
            "timestamp": self.timestamp,
            "type": self.event_type,
            "scenario": self.scenario_id,
            "model_profile": self.model_profile,
            "turn": self.turn,
            "correlation_id": self.correlation_id,
            "taints": list(self.taints),
            "redactions": list(self.redactions),
            "facts": _copy_json(self.facts),
        }


class TraceRecorder:
    """Build a single ordered trace while redacting registered sensitive values."""

    def __init__(
        self,
        run_id: str,
        scenario_id: str,
        model_profile: str,
        *,
        sensitive_values: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.run_id = _checked_identifier(run_id, "run id")
        self.scenario_id = _checked_identifier(scenario_id, "scenario id")
        self.model_profile = _checked_identifier(model_profile, "model profile")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._events: list[TraceEvent] = []
        checked: list[tuple[str, str]] = []
        for label, value in (sensitive_values or {}).items():
            checked_label = _checked_identifier(label, "redaction label")
            if (
                not isinstance(value, str)
                or not value
                or len(value.encode("utf-8")) > MAX_STRING_BYTES
            ):
                raise TraceValidationError(
                    "sensitive values must be non-empty bounded strings"
                )
            checked.append((checked_label, value))
        # Replace longer values first so one secret cannot expose a suffix of another.
        self._sensitive_values = tuple(
            sorted(checked, key=lambda item: len(item[1]), reverse=True)
        )

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        return tuple(self._events)

    def emit(
        self,
        event_type: str,
        *,
        facts: Mapping[str, object] | None = None,
        turn: int | None = None,
        correlation_id: str | None = None,
        taints: Sequence[Taint | str] = (),
    ) -> TraceEvent:
        checked_type = _checked_identifier(event_type, "event type")
        if turn is not None and (type(turn) is not int or turn < 0):
            raise TraceValidationError("turn must be a non-negative integer or null")
        checked_correlation = None
        if correlation_id is not None:
            checked_correlation = _checked_identifier(correlation_id, "correlation id")
        checked_taints = _checked_taints(taints)
        redactions: set[str] = set()
        normalized = _normalize_facts(facts or {}, self._sensitive_values, redactions)
        if not isinstance(
            normalized, dict
        ):  # Defensive: the root contract is always an object.
            raise TraceValidationError("trace facts must be an object")
        timestamp = self._clock()
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise TraceValidationError(
                "trace clock must return a timezone-aware datetime"
            )
        rendered_timestamp = (
            timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        event = TraceEvent(
            run_id=self.run_id,
            sequence=len(self._events) + 1,
            timestamp=rendered_timestamp,
            event_type=checked_type,
            scenario_id=self.scenario_id,
            model_profile=self.model_profile,
            turn=turn,
            correlation_id=checked_correlation,
            taints=checked_taints,
            redactions=tuple(sorted(redactions)),
            facts=_freeze(normalized),
        )
        encoded = json.dumps(
            event.as_dict(), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(encoded) > MAX_EVENT_BYTES:
            raise TraceValidationError("trace event is oversized")
        self._events.append(event)
        return event

    def to_jsonl(self) -> str:
        return "".join(
            json.dumps(
                event.as_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
            for event in self._events
        )


def validate_event_stream(events: Sequence[TraceEvent]) -> None:
    """Reject spliced, reordered, or mixed-run event sequences."""
    if not events:
        raise TraceValidationError("trace event stream is empty")
    first = events[0]
    if not isinstance(first, TraceEvent):
        raise TraceValidationError("trace contains an invalid event")
    for expected, event in enumerate(events, start=1):
        if not isinstance(event, TraceEvent):
            raise TraceValidationError("trace contains an invalid event")
        if (
            event.sequence != expected
            or event.run_id != first.run_id
            or event.scenario_id != first.scenario_id
            or event.model_profile != first.model_profile
        ):
            raise TraceValidationError("trace event stream is not contiguous")


def _checked_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise TraceValidationError(f"{label} is invalid")
    return value


def _checked_taints(values: Sequence[Taint | str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TraceValidationError("taints must be a sequence")
    allowed = {item.value for item in Taint}
    checked: list[str] = []
    for item in values:
        value = item.value if isinstance(item, Taint) else item
        if not isinstance(value, str) or value not in allowed:
            raise TraceValidationError("trace contains an unknown taint")
        if value not in checked:
            checked.append(value)
    return tuple(checked)


def _normalize_facts(
    value: object,
    sensitive_values: tuple[tuple[str, str], ...],
    redactions: set[str],
    *,
    depth: int = 0,
) -> object:
    if depth > MAX_FACT_DEPTH:
        raise TraceValidationError("trace facts are too deeply nested")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value.bit_length() > 256:
            raise TraceValidationError("trace fact integer is oversized")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TraceValidationError("trace facts contain a non-finite number")
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_STRING_BYTES:
            raise TraceValidationError("trace fact string is oversized")
        return _redact_text(value, sensitive_values, redactions)
    if isinstance(value, Mapping):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise TraceValidationError("trace fact object has too many fields")
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 128:
                raise TraceValidationError("trace fact keys must be bounded strings")
            checked_key = _redact_text(key, sensitive_values, redactions)
            if checked_key in result:
                raise TraceValidationError("redaction makes trace fact keys ambiguous")
            result[checked_key] = _normalize_facts(
                item, sensitive_values, redactions, depth=depth + 1
            )
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise TraceValidationError("trace fact array has too many items")
        return [
            _normalize_facts(item, sensitive_values, redactions, depth=depth + 1)
            for item in value
        ]
    raise TraceValidationError("trace facts must contain only JSON values")


def _redact_text(
    value: str,
    sensitive_values: tuple[tuple[str, str], ...],
    redactions: set[str],
) -> str:
    masked = value
    for label, secret in sensitive_values:
        if secret in masked:
            masked = masked.replace(secret, f"[REDACTED:{label}]")
            redactions.add(label)
    masked, count = _CREDENTIAL.subn("[REDACTED:credential]", masked)
    if count:
        redactions.add("credential")
    return masked


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _copy_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _copy_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy_json(item) for item in value]
    return value


__all__ = [
    "MAX_EVENT_BYTES",
    "Taint",
    "TraceEvent",
    "TraceRecorder",
    "TraceValidationError",
    "validate_event_stream",
]
