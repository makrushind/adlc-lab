"""Opt-in composition of the weak guardrail with the existing boundary pipeline."""

from aiweekend_target.core.contracts import PrivateEvidenceSink, PublicEventSink
from aiweekend_target.core.pipeline import BoundaryPipeline

from .adapters import GuardrailBridge
from .contracts import GuardrailRunContext
from .engine import evaluate


def create_pipeline(
    context: GuardrailRunContext,
    *,
    mode: str,
    event_sink: PublicEventSink | None = None,
    evidence_sink: PrivateEvidenceSink | None = None,
) -> BoundaryPipeline:
    """Create one paired analyzer/policy for one sequential agent session.

    Mode must be explicitly selected as observe or enforce. This factory does
    not register global components or change any existing policy profile.
    """
    bridge = GuardrailBridge(evaluate, context, mode=mode)
    return BoundaryPipeline(
        analyzers=(bridge.analyzer,),
        policy=bridge.policy,
        event_sink=event_sink,
        evidence_sink=evidence_sink,
    )
