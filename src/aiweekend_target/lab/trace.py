"""Bounded JSON trace facts for the transparent attack-lab agent."""

from __future__ import annotations

import re
from collections.abc import Mapping


CANARIES = (
    "ADLC_CANARY_RAG_7A91C4",
    "ADLC_CANARY_MCP_4DB2E8",
    "ADLC_CANARY_LLM_C61F03",
    "ADLC_CANARY_CUSTOM_95A7D2",
)
_STAGES = ("prompt", "rag", "mcp", "llm", "agent")
_SECRET_PREVIEW = re.compile(
    r"(?i)\b(?:authorization|proxy-authorization|x-api-key|api-key|cookie|set-cookie|x-auth-token|token|secret|password)\s*[:=]\s*[^\r\n]+|\bbearer\s+[^\s,;]+|\bhf_[A-Za-z0-9_-]+"
)


def canaries_in(value: object, *, allowed: tuple[str, ...] = CANARIES) -> list[str]:
    """Return the fixed, ordered canaries present in a bounded fact."""
    found: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, str):
            found.update(canary for canary in allowed if canary in item)
        elif isinstance(item, Mapping):
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return [canary for canary in allowed if canary in found]


def safe_preview(value: str, maximum: int) -> str:
    """Mask credentials and fixed canaries before bounding an untrusted trace preview."""
    masked = value
    for canary in CANARIES:
        masked = masked.replace(canary, "[CANARY]")
    return _SECRET_PREVIEW.sub("[REDACTED]", masked)[:maximum]


class TraceObserver:
    """Produce only normalized, non-secret events and high-level stage facts."""

    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self._stages: list[str] = []
        self._canaries: set[str] = set()

    def emit(
        self,
        event_type: str,
        *,
        stage: str | None = None,
        canaries: list[str] | tuple[str, ...] = (),
        **facts: object,
    ) -> dict[str, object]:
        if stage in _STAGES and stage not in self._stages:
            self._stages.append(stage)
        filtered = [canary for canary in CANARIES if canary in canaries]
        self._canaries.update(filtered)
        return {
            "schema": 1,
            "type": event_type,
            "scenario": self.scenario,
            **facts,
            "canaries": filtered,
        }

    def result(self, ok: bool) -> dict[str, object]:
        stages = list(self._stages)
        return {
            "schema": 1,
            "type": "lab_result",
            "ok": bool(ok and stages == list(_STAGES)),
            "scenario": self.scenario,
            "stages": stages,
            "canaries": [canary for canary in CANARIES if canary in self._canaries],
        }


__all__ = ["CANARIES", "TraceObserver", "canaries_in", "safe_preview"]
