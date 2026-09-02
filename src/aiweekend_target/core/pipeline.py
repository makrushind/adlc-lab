"""Ordered transforms, analyzers and host policy for every agent boundary."""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from dataclasses import dataclass

from aiweekend_target.core.contracts import (
    Analyzer,
    AttackTransform,
    BoundaryContext,
    BoundaryEvidence,
    ControlAction,
    ControlDecision,
    ControlPolicy,
    Finding,
    PrivateEvidenceSink,
    PublicEventSink,
    TransformOutcome,
)


class BoundaryPipelineError(RuntimeError):
    """A trusted component failed or returned data outside its contract."""


@dataclass(frozen=True)
class BoundaryOutcome:
    context: BoundaryContext
    findings: tuple[Finding, ...]
    decision: ControlDecision
    delivered_payload: object | None


class AllowPolicy:
    id = "core.allow"

    def decide(
        self, context: BoundaryContext, findings: Sequence[Finding]
    ) -> ControlDecision:
        del context, findings
        return ControlDecision(ControlAction.ALLOW, "policy.allow")


class FindingPolicy:
    """Generic observe/enforce policy keyed only by finding codes."""

    id = "core.finding-policy"

    def __init__(
        self,
        *,
        blocked_codes: Sequence[str],
        enforce: bool,
        replacements: dict[str, object] | None = None,
    ) -> None:
        self._blocked = frozenset(blocked_codes)
        self._enforce = enforce
        self._replacements = dict(replacements or {})

    def decide(
        self, context: BoundaryContext, findings: Sequence[Finding]
    ) -> ControlDecision:
        matched = sorted({item.code for item in findings} & self._blocked)
        if not matched or not self._enforce:
            return ControlDecision(
                ControlAction.ALLOW,
                "policy.observed" if matched else "policy.allow",
            )
        replacement = self._replacements.get(context.boundary.value)
        if replacement is not None:
            return ControlDecision(ControlAction.REPLACE, "policy.replace", replacement)
        return ControlDecision(ControlAction.BLOCK, "policy.finding")


async def _await(value: object) -> object:
    return await value if inspect.isawaitable(value) else value


class BoundaryPipeline:
    """Run trusted components in deterministic order and retain private evidence."""

    def __init__(
        self,
        *,
        transforms: Sequence[AttackTransform] = (),
        analyzers: Sequence[Analyzer] = (),
        policy: ControlPolicy | None = None,
        event_sink: PublicEventSink | None = None,
        evidence_sink: PrivateEvidenceSink | None = None,
    ) -> None:
        self._transforms = tuple(transforms)
        self._analyzers = tuple(analyzers)
        self._policy = policy or AllowPolicy()
        self._event_sink = event_sink
        self._evidence_sink = evidence_sink
        identifiers = [item.id for item in (*self._transforms, *self._analyzers, self._policy)]
        if any(not isinstance(item, str) or not item for item in identifiers):
            raise ValueError("pipeline component id is invalid")

    def _emit(self, event_type: str, context: BoundaryContext, **facts: object) -> None:
        if self._event_sink is not None:
            self._event_sink(
                {
                    "schema": 3,
                    "type": event_type,
                    "boundary": context.boundary.value,
                    "user_turn": context.user_turn,
                    "model_turn": context.model_turn,
                    "correlation_id": context.correlation_id,
                    "tool": context.tool_name,
                    **facts,
                }
            )

    async def process(self, context: BoundaryContext) -> BoundaryOutcome:
        original = context.payload
        current = context
        for transform in self._transforms:
            try:
                raw = await _await(transform.transform(current))
                if not isinstance(raw, TransformOutcome):
                    raise TypeError("attack transform returned an invalid outcome")
                if raw.applied:
                    current = current.with_payload(raw.payload, provenance=raw.provenance)
                self._emit(
                    "attack_transform",
                    current,
                    component=transform.id,
                    outcome="applied" if raw.applied else "skipped",
                )
            except Exception as error:
                self._emit("control_failure", current, component=transform.id)
                raise BoundaryPipelineError("attack transform failed closed") from error
        findings: list[Finding] = []
        for analyzer in self._analyzers:
            try:
                raw_findings = await _await(analyzer.analyze(current))
                if not isinstance(raw_findings, Sequence):
                    raise TypeError("analyzer returned an invalid finding collection")
                for finding in raw_findings:
                    if not isinstance(finding, Finding) or finding.analyzer_id != analyzer.id:
                        raise TypeError("analyzer returned an invalid finding")
                    findings.append(finding)
                    self._emit(
                        "analyzer_finding",
                        current,
                        component=analyzer.id,
                        code=finding.code,
                        severity=finding.severity,
                    )
            except Exception as error:
                self._emit("control_failure", current, component=analyzer.id)
                raise BoundaryPipelineError("analyzer failed closed") from error
        try:
            raw_decision = await _await(self._policy.decide(current, tuple(findings)))
            if not isinstance(raw_decision, ControlDecision):
                raise TypeError("control policy returned an invalid decision")
            decision = raw_decision
        except Exception as error:
            self._emit("control_failure", current, component=self._policy.id)
            raise BoundaryPipelineError("control policy failed closed") from error

        if decision.action is ControlAction.ALLOW:
            delivered = current.payload
        elif decision.action is ControlAction.REPLACE:
            delivered = decision.replacement
        else:
            delivered = None
        self._emit(
            "control_decision",
            current,
            component=self._policy.id,
            action=decision.action.value,
            reason=decision.reason,
        )
        evidence = BoundaryEvidence(
            boundary=current.boundary,
            user_turn=current.user_turn,
            model_turn=current.model_turn,
            correlation_id=current.correlation_id,
            tool_name=current.tool_name,
            original_payload=original,
            delivered_payload=delivered,
            provenance=current.provenance,
            findings=tuple(findings),
            decision=decision,
        )
        if self._evidence_sink is not None:
            try:
                self._evidence_sink(evidence)
            except Exception as error:
                self._emit("control_failure", current, component="core.evidence-sink")
                raise BoundaryPipelineError("private evidence sink failed closed") from error
        return BoundaryOutcome(current, tuple(findings), decision, delivered)


__all__ = [
    "AllowPolicy",
    "BoundaryOutcome",
    "BoundaryPipeline",
    "BoundaryPipelineError",
    "FindingPolicy",
]
