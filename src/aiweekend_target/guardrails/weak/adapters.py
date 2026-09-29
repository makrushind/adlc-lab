"""Run-local bridge between guardrail evaluation and the core boundary pipeline."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence

from aiweekend_target.core.contracts import (
    BoundaryContext,
    ControlAction,
    ControlDecision,
    Finding,
)

from .contracts import (
    BOUNDARIES,
    GuardrailEvent,
    GuardrailResult,
    GuardrailRunContext,
    Source,
    encoded,
    identifier,
    thaw,
    validate_diagnostics,
)


def guardrail_event(context: BoundaryContext) -> GuardrailEvent:
    payload = context.payload
    if isinstance(payload, str):
        kind = "active" if context.boundary.value == "input" else "output"
        sources = (Source(kind, kind, payload),)
    elif isinstance(payload, (dict, list)):

        def fields(value: object, path: str):
            if isinstance(value, str):
                yield Source(path, "field", value)
            elif isinstance(value, dict):
                for index, (key, item) in enumerate(value.items()):
                    candidate = f"{path}.{key}"
                    child = (
                        candidate if identifier(candidate) else f"field.item-{index}"
                    )
                    yield from fields(item, child)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    yield from fields(item, f"{path}.item-{index}")

        sources = tuple(fields(payload, "field"))
    else:
        sources = ()
    return GuardrailEvent(context.boundary.value, payload, sources, context.tool_name)


def _identity(context: BoundaryContext) -> tuple[object, ...]:
    return (
        context.boundary,
        context.run_id,
        context.user_turn,
        context.model_turn,
        context.correlation_id,
        context.tool_name,
        hashlib.sha256(encoded(context.payload)).hexdigest(),
    )


class GuardrailBridge:
    """A single-use decision slot shared by one session's analyzer and policy.

    Keep the pair in the same sequential pipeline; do not share it across
    concurrent sessions. Diagnostics retain raw private payloads in memory
    for this bridge's lifetime and must not be sent to the public event sink.
    """

    def __init__(
        self,
        evaluate: Callable[[GuardrailEvent, GuardrailRunContext], GuardrailResult],
        context: GuardrailRunContext,
        *,
        mode: str = "enforce",
    ) -> None:
        if mode not in {"observe", "enforce"}:
            raise ValueError("invalid enforcement mode")
        self.evaluate = evaluate
        self.context = context
        self.mode = mode
        self._pending: tuple[tuple[object, ...], GuardrailResult] | None = None
        self.diagnostics: list[tuple[GuardrailEvent, GuardrailResult]] = []
        self.analyzer = GuardrailAnalyzer(self)
        self.policy = GuardrailPolicy(self)


class GuardrailAnalyzer:
    id = "guardrail.weak.analyzer"

    def __init__(self, bridge: GuardrailBridge) -> None:
        self.bridge = bridge

    def analyze(self, context: BoundaryContext) -> tuple[Finding, ...]:
        if context.boundary.value not in BOUNDARIES:
            return ()
        if self.bridge._pending is not None:
            self.bridge._pending = None
            raise ValueError("unconsumed guardrail decision")
        event = guardrail_event(context)
        result = self.bridge.evaluate(event, self.bridge.context)
        if not isinstance(result, GuardrailResult):
            raise TypeError("evaluate must return GuardrailResult")
        validate_diagnostics(result, self.bridge.context.protected_values)
        if result.action == "replace" and event.boundary not in {
            "tool_result",
            "final_output",
        }:
            raise ValueError(
                "replacement is only supported for results and final output"
            )
        if (
            result.action == "replace"
            and event.boundary == "final_output"
            and (
                not isinstance(result.replacement, str)
                or not result.replacement.strip()
            )
        ):
            raise ValueError("final output replacement must be nonblank text")
        self.bridge._pending = (_identity(context), result)
        self.bridge.diagnostics.append((event, result))
        return tuple(
            Finding(self.id, f"guardrail.weak.{signal.code.lower()}", "medium")
            for signal in result.signals
        )


class GuardrailPolicy:
    id = "guardrail.weak.policy"

    def __init__(self, bridge: GuardrailBridge) -> None:
        self.bridge = bridge

    def decide(
        self, context: BoundaryContext, findings: Sequence[Finding]
    ) -> ControlDecision:
        if context.boundary.value not in BOUNDARIES:
            return ControlDecision(ControlAction.ALLOW, "guardrail.weak.provider_boundary")
        pending, self.bridge._pending = self.bridge._pending, None
        if pending is None or pending[0] != _identity(context):
            raise ValueError("missing or mismatched guardrail decision")
        result = pending[1]
        if self.bridge.mode == "observe":
            return ControlDecision(ControlAction.ALLOW, "guardrail.weak.observed")
        return ControlDecision(
            ControlAction(result.action),
            f"guardrail.weak.{result.reason.lower()}",
            thaw(result.replacement),
        )
