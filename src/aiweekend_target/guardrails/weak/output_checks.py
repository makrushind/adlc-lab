"""Tool results and final output pass through without content or leakage checks."""

from .contracts import GuardrailResult


def pass_through(event, context):
    return GuardrailResult("allow", "content.safe")
