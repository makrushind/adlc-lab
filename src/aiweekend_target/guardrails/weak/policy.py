"""Review uses the first signal or route alone; combined signals do not deny access."""

from .contracts import GuardrailResult, ReviewResult


def decide(signals, context):
    if signals:
        return ReviewResult("BLOCK", signals[0].code, signals)
    reason = {
        "account_safety": "ACCOUNT_SAFETY",
        "appeal": "APPEAL_DECISION",
        "policy": "POLICY_QUESTION",
    }.get(context["route"], "ORDINARY_SUPPORT")
    return ReviewResult("ALLOW", reason)


def combine(signals):
    """Content signals alone do not enforce authorization in this implementation."""
    return GuardrailResult("allow", "content.safe", tuple(signals))
