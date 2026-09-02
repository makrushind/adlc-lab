"""Provider-neutral tool contracts used by autonomous agent runners.

The model only receives the public :class:`ToolSpec` values.  Executable
handlers and optional validators stay in the trusted host process.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeAlias

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from aiweekend_target.agent_protocol import strict_json


MAX_TOOL_NAME_BYTES = 64
MAX_TOOL_DESCRIPTION_BYTES = 4_096
MAX_TOOL_SCHEMA_BYTES = 32 * 1024
MAX_TOOL_ARGUMENT_BYTES = 16 * 1024
MAX_TOOL_RESULT_BYTES = 96 * 1024
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_UNSAFE_SCHEMA_KEYWORDS = frozenset(
    {
        "$dynamicRef",
        "$ref",
        "allOf",
        "anyOf",
        "contains",
        "contentEncoding",
        "contentMediaType",
        "dependentSchemas",
        "else",
        "format",
        "if",
        "not",
        "oneOf",
        "pattern",
        "patternProperties",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)


class ToolError(RuntimeError):
    """Base class for a rejected or failed tool boundary operation."""


class ToolProtocolError(ToolError):
    """Tool metadata, arguments, or a result violated the local contract."""


class UnknownToolError(ToolError):
    """The model requested a tool outside the advertised allowlist."""


class ToolExecutionError(ToolError):
    """An advertised tool failed while executing."""


def _canonical_json(value: object, *, maximum: int, label: str) -> tuple[object, str]:
    """Return a detached strict-JSON value and its bounded canonical form."""
    try:
        document = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        size = len(document.encode("utf-8"))
        if size > maximum:
            raise ToolProtocolError(f"{label} exceeds the byte limit")
        detached = strict_json(document)
    except ToolProtocolError:
        raise
    except (TypeError, ValueError, UnicodeError) as error:
        raise ToolProtocolError(f"{label} must be strict JSON") from error
    return detached, document


def validate_tool_arguments(value: object) -> dict[str, object]:
    """Validate and detach one bounded JSON-object argument set."""
    detached, _ = _canonical_json(value, maximum=MAX_TOOL_ARGUMENT_BYTES, label="tool arguments")
    if not isinstance(detached, dict) or any(not isinstance(key, str) for key in detached):
        raise ToolProtocolError("tool arguments must be a JSON object")
    validate_tool_json_value(detached, label="tool arguments")
    return detached


def validate_tool_json_value(
    value: object, *, label: str = "tool value", depth: int = 0
) -> None:
    """Enforce the evaluator's bounded JSON evidence shape before side effects."""
    if depth > 8:
        raise ToolProtocolError(f"{label} is too deeply nested")
    if value is None or isinstance(value, bool | int | float | str):
        return
    if isinstance(value, Mapping):
        if (
            len(value) > 256
            or not all(isinstance(key, str) for key in value)
            or any(len(key.encode("utf-8")) > 4_096 for key in value)
        ):
            raise ToolProtocolError(f"{label} contains an invalid object")
        for item in value.values():
            validate_tool_json_value(item, label=label, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > 256:
            raise ToolProtocolError(f"{label} contains an oversized array")
        for item in value:
            validate_tool_json_value(item, label=label, depth=depth + 1)
        return
    raise ToolProtocolError(f"{label} must contain JSON-compatible values")


def _validate_safe_schema(value: object, *, depth: int = 0) -> None:
    if depth > 16:
        raise ToolProtocolError("tool input schema is too deeply nested")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _UNSAFE_SCHEMA_KEYWORDS:
                raise ToolProtocolError(
                    f"tool input schema uses unsupported keyword: {key}"
                )
            _validate_safe_schema(item, depth=depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            _validate_safe_schema(item, depth=depth + 1)


def validate_tool_arguments_against_schema(
    value: object, schema: Mapping[str, object]
) -> dict[str, object]:
    """Validate bounded arguments against the exact advertised safe schema."""
    checked = validate_tool_arguments(value)
    try:
        Draft202012Validator(schema).validate(checked)
    except (SchemaError, ValidationError) as error:
        raise ToolProtocolError(
            "tool arguments do not match the advertised input schema"
        ) from error
    return checked


def serialize_tool_result(value: object, *, maximum: int = MAX_TOOL_RESULT_BYTES) -> str:
    """Serialize a tool result for an assistant message without leaking Python types."""
    detached, document = _canonical_json(value, maximum=maximum, label="tool result")
    validate_tool_json_value(detached, label="tool result")
    return document


@dataclass(frozen=True)
class ToolSpec:
    """The model-visible part of one trusted executable tool."""

    name: str
    description: str
    input_schema: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _TOOL_NAME.fullmatch(self.name):
            raise ToolProtocolError("tool name is invalid")
        if len(self.name.encode("utf-8")) > MAX_TOOL_NAME_BYTES:
            raise ToolProtocolError("tool name exceeds the byte limit")
        if not isinstance(self.description, str) or len(self.description.encode("utf-8")) > MAX_TOOL_DESCRIPTION_BYTES:
            raise ToolProtocolError("tool description is invalid")
        detached, _ = _canonical_json(
            self.input_schema,
            maximum=MAX_TOOL_SCHEMA_BYTES,
            label="tool input schema",
        )
        if not isinstance(detached, dict) or detached.get("type") != "object":
            raise ToolProtocolError("tool input schema must describe an object")
        _validate_safe_schema(detached)
        try:
            Draft202012Validator.check_schema(detached)
        except SchemaError as error:
            raise ToolProtocolError("tool input schema is invalid") from error
        object.__setattr__(self, "input_schema", detached)

    def as_openai_tool(self) -> dict[str, object]:
        """Return an isolated OpenAI-compatible function-tool declaration."""
        schema, _ = _canonical_json(
            self.input_schema,
            maximum=MAX_TOOL_SCHEMA_BYTES,
            label="tool input schema",
        )
        function: dict[str, object] = {"name": self.name, "parameters": schema}
        if self.description:
            function["description"] = self.description
        return {"type": "function", "function": function}


class ToolProvider(Protocol):
    """A trusted source of an allowlisted tool surface."""

    async def list_tools(self) -> Sequence[ToolSpec]: ...

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object: ...


ToolHandler: TypeAlias = Callable[[dict[str, object]], object | Awaitable[object]]
ArgumentValidator: TypeAlias = Callable[[object], Mapping[str, object]]
ResultValidator: TypeAlias = Callable[[object], object]


@dataclass(frozen=True)
class _RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler
    argument_validator: ArgumentValidator | None
    result_validator: ResultValidator | None


class ToolRegistry:
    """An in-process allowlist suitable for tests and deterministic host tools."""

    def __init__(self, *, max_result_bytes: int = MAX_TOOL_RESULT_BYTES) -> None:
        if type(max_result_bytes) is not int or max_result_bytes < 1:
            raise ValueError("max_result_bytes must be a positive integer")
        self._max_result_bytes = max_result_bytes
        self._tools: dict[str, _RegisteredTool] = {}

    def register(
        self,
        spec: ToolSpec,
        handler: ToolHandler,
        *,
        argument_validator: ArgumentValidator | None = None,
        result_validator: ResultValidator | None = None,
    ) -> None:
        if spec.name in self._tools:
            raise ToolProtocolError(f"duplicate tool: {spec.name}")
        if not callable(handler):
            raise ToolProtocolError("tool handler must be callable")
        self._tools[spec.name] = _RegisteredTool(spec, handler, argument_validator, result_validator)

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return tuple(item.spec for item in self._tools.values())

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        item = self._tools.get(name)
        if item is None:
            raise UnknownToolError(f"tool is not allowlisted: {name}")
        checked = validate_tool_arguments(arguments)
        if item.argument_validator is not None:
            try:
                checked = validate_tool_arguments(item.argument_validator(checked))
            except ToolError:
                raise
            except Exception as error:
                raise ToolProtocolError(f"tool arguments were rejected: {name}") from error
        try:
            result = item.handler(checked)
            if inspect.isawaitable(result):
                result = await result
        except ToolError:
            raise
        except Exception as error:
            raise ToolExecutionError(f"tool execution failed: {name}") from error
        if item.result_validator is not None:
            try:
                result = item.result_validator(result)
            except ToolError:
                raise
            except Exception as error:
                raise ToolProtocolError(f"tool result was rejected: {name}") from error
        document = serialize_tool_result(result, maximum=self._max_result_bytes)
        return strict_json(document)


__all__ = [
    "ArgumentValidator",
    "MAX_TOOL_ARGUMENT_BYTES",
    "MAX_TOOL_RESULT_BYTES",
    "ResultValidator",
    "ToolError",
    "ToolExecutionError",
    "ToolHandler",
    "ToolProtocolError",
    "ToolProvider",
    "ToolRegistry",
    "ToolSpec",
    "UnknownToolError",
    "serialize_tool_result",
    "validate_tool_arguments",
    "validate_tool_arguments_against_schema",
    "validate_tool_json_value",
]
