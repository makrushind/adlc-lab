"""Trusted reference adapters installed by the Plan 1 composition root."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from aiweekend_target.core import (
    Boundary,
    BoundaryContext,
    ComponentCatalog,
    ComponentKind,
    Finding,
    FindingPolicy,
    TransformOutcome,
)
from aiweekend_target.lab.evaluation_v3 import DeclarativeEvaluator
from aiweekend_target.tools import ToolRegistry, ToolSpec


def _exact(config: Mapping[str, object], fields: set[str], label: str) -> None:
    if set(config) != fields:
        raise ValueError(f"{label} config has unexpected fields")


class TextTransform:
    id = "builtin.text-transform"

    def __init__(self, config: Mapping[str, object]) -> None:
        _exact(config, {"boundary", "operation", "text", "field", "target_tool"}, "text transform")
        try:
            self._boundary = Boundary(config["boundary"])
        except (KeyError, ValueError) as error:
            raise ValueError("text transform boundary is invalid") from error
        operation = config.get("operation")
        text = config.get("text")
        field = config.get("field")
        target_tool = config.get("target_tool")
        if operation not in {"append", "prepend", "replace"}:
            raise ValueError("text transform operation is invalid")
        if not isinstance(text, str) or not text or len(text.encode()) > 32 * 1024:
            raise ValueError("text transform payload is invalid")
        if field is not None and (
            not isinstance(field, str) or not field or len(field.encode()) > 128
        ):
            raise ValueError("text transform field is invalid")
        if target_tool is not None and (
            not isinstance(target_tool, str) or not target_tool
        ):
            raise ValueError("text transform target tool is invalid")
        self._operation = operation
        self._text = text
        self._field = field
        self._target_tool = target_tool

    def _change(self, value: str) -> str:
        if self._operation == "append":
            return value + self._text
        if self._operation == "prepend":
            return self._text + value
        return self._text

    def transform(self, context: BoundaryContext) -> TransformOutcome:
        if context.boundary is not self._boundary or (
            self._target_tool is not None and context.tool_name != self._target_tool
        ):
            return TransformOutcome(context.payload, False)
        if self._field is None and isinstance(context.payload, str):
            return TransformOutcome(
                self._change(context.payload), True, ("attack.text-transform",)
            )
        if self._field is not None and isinstance(context.payload, dict):
            current = context.payload.get(self._field)
            if isinstance(current, str):
                changed = dict(context.payload)
                changed[self._field] = self._change(current)
                return TransformOutcome(changed, True, ("attack.text-transform",))
        return TransformOutcome(context.payload, False)


class PatternAnalyzer:
    id = "builtin.pattern"

    def __init__(self, config: Mapping[str, object]) -> None:
        _exact(config, {"boundaries", "patterns", "code", "severity", "case_sensitive"}, "pattern analyzer")
        boundaries = config.get("boundaries")
        patterns = config.get("patterns")
        code = config.get("code")
        severity = config.get("severity")
        case_sensitive = config.get("case_sensitive")
        if not isinstance(boundaries, list) or not boundaries:
            raise ValueError("pattern analyzer boundaries are invalid")
        try:
            self._boundaries = frozenset(Boundary(item) for item in boundaries)
        except (TypeError, ValueError) as error:
            raise ValueError("pattern analyzer boundaries are invalid") from error
        if (
            not isinstance(patterns, list)
            or not patterns
            or len(patterns) > 64
            or any(not isinstance(item, str) or not item for item in patterns)
        ):
            raise ValueError("pattern analyzer patterns are invalid")
        if not isinstance(code, str) or not code:
            raise ValueError("pattern analyzer code is invalid")
        if severity not in {"info", "low", "medium", "high", "critical"}:
            raise ValueError("pattern analyzer severity is invalid")
        if type(case_sensitive) is not bool:
            raise ValueError("pattern analyzer case flag is invalid")
        self._patterns = tuple(patterns)
        self._code = code
        self._severity = severity
        self._case_sensitive = case_sensitive

    def analyze(self, context: BoundaryContext) -> Sequence[Finding]:
        if context.boundary not in self._boundaries:
            return ()
        document = json.dumps(context.payload, ensure_ascii=False, sort_keys=True)
        haystack = document if self._case_sensitive else document.casefold()
        patterns = self._patterns if self._case_sensitive else tuple(
            item.casefold() for item in self._patterns
        )
        if any(item in haystack for item in patterns):
            return (Finding(self.id, self._code, self._severity),)
        return ()


class NumericLimitAnalyzer:
    id = "builtin.numeric-limit"

    def __init__(self, config: Mapping[str, object]) -> None:
        _exact(config, {"boundary", "field", "maximum", "code", "severity"}, "numeric analyzer")
        try:
            self._boundary = Boundary(config["boundary"])
        except (KeyError, ValueError) as error:
            raise ValueError("numeric analyzer boundary is invalid") from error
        self._field = config.get("field")
        self._maximum = config.get("maximum")
        self._code = config.get("code")
        self._severity = config.get("severity")
        if not isinstance(self._field, str) or not self._field:
            raise ValueError("numeric analyzer field is invalid")
        if isinstance(self._maximum, bool) or not isinstance(self._maximum, int | float):
            raise ValueError("numeric analyzer maximum is invalid")
        if not isinstance(self._code, str) or not self._code:
            raise ValueError("numeric analyzer code is invalid")
        if self._severity not in {"info", "low", "medium", "high", "critical"}:
            raise ValueError("numeric analyzer severity is invalid")

    def analyze(self, context: BoundaryContext) -> Sequence[Finding]:
        if context.boundary is not self._boundary or not isinstance(context.payload, dict):
            return ()
        value = context.payload.get(self._field)
        if not isinstance(value, bool) and isinstance(value, int | float) and value > self._maximum:
            return (Finding(self.id, self._code, self._severity),)
        return ()


def fixture_provider(config: Mapping[str, object]) -> ToolRegistry:
    _exact(config, {"tools"}, "fixture provider")
    tools = config.get("tools")
    if not isinstance(tools, list) or not tools or len(tools) > 32:
        raise ValueError("fixture provider tools are invalid")
    registry = ToolRegistry()
    for item in tools:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "description",
            "input_schema",
            "result",
        }:
            raise ValueError("fixture tool has unexpected fields")
        spec = ToolSpec(
            item.get("name"),
            item.get("description"),
            item.get("input_schema"),
        )
        result = item.get("result")
        registry.register(spec, lambda arguments, value=result: value)
    return registry


def arithmetic_provider(config: Mapping[str, object]) -> ToolRegistry:
    _exact(config, set(), "arithmetic provider")
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "multiply_numbers",
            "Multiply two numbers and return their product.",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "left": {"type": "number"},
                    "right": {"type": "number"},
                },
                "required": ["left", "right"],
            },
        ),
        lambda arguments: {"product": arguments["left"] * arguments["right"]},
    )
    return registry


def finding_policy(config: Mapping[str, object]) -> FindingPolicy:
    _exact(config, {"enforce", "blocked_codes", "replacements"}, "finding policy")
    enforce = config.get("enforce")
    blocked = config.get("blocked_codes")
    replacements = config.get("replacements")
    if type(enforce) is not bool:
        raise ValueError("finding policy enforce flag is invalid")
    if not isinstance(blocked, list) or any(not isinstance(item, str) for item in blocked):
        raise ValueError("finding policy blocked codes are invalid")
    if not isinstance(replacements, dict):
        raise ValueError("finding policy replacements are invalid")
    return FindingPolicy(
        blocked_codes=blocked,
        enforce=enforce,
        replacements=replacements,
    )


def install_builtin_components(
    catalog: ComponentCatalog,
    *,
    model_factories: Mapping[str, object],
) -> None:
    """Install a finite allowlist. Runtime/provider secrets stay in captured factories."""
    for component_id, factory in model_factories.items():
        if not callable(factory):
            raise ValueError("model factory is invalid")
        catalog.register(ComponentKind.MODEL, component_id, "1", factory)
    catalog.register(ComponentKind.TOOL_PROVIDER, "builtin.fixture", "1", fixture_provider)
    catalog.register(ComponentKind.TOOL_PROVIDER, "builtin.arithmetic", "1", arithmetic_provider)
    catalog.register(ComponentKind.ATTACK, "builtin.text-transform", "1", TextTransform)
    catalog.register(ComponentKind.ANALYZER, "builtin.pattern", "1", PatternAnalyzer)
    catalog.register(ComponentKind.ANALYZER, "builtin.numeric-limit", "1", NumericLimitAnalyzer)
    catalog.register(ComponentKind.POLICY, "builtin.findings", "1", finding_policy)
    catalog.register(
        ComponentKind.EVALUATOR,
        "builtin.assertions",
        "1",
        lambda config: DeclarativeEvaluator() if not config else (_ for _ in ()).throw(
            ValueError("assertion evaluator config must be empty")
        ),
    )


__all__ = [
    "NumericLimitAnalyzer",
    "PatternAnalyzer",
    "TextTransform",
    "arithmetic_provider",
    "finding_policy",
    "fixture_provider",
    "install_builtin_components",
]
