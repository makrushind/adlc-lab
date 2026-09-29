"""Intentionally incomplete boundary evaluation and standalone content review."""

from .authorization import visible_grant
from .contracts import GuardrailResult
from .detectors import narrow_signals
from .output_checks import pass_through
from .policy import decide


def review(request):
    return decide(
        narrow_signals(" ".join((request.message, *request.evidence))), request.context
    )


def evaluate(event, context):
    if event.boundary == "input":
        found = narrow_signals(str(event.payload))
        return (
            GuardrailResult("block", "prompt.override", found)
            if found
            else GuardrailResult("allow", "content.safe")
        )
    if event.boundary == "tool_call":
        return (
            GuardrailResult("allow", "authorized")
            if visible_grant(event, context)
            else GuardrailResult("block", "authorization.denied")
        )
    return pass_through(event, context)
