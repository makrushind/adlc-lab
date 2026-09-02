"""A narrow MCP-to-agent tool adapter with an explicit allowlist."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Protocol

from aiweekend_target.tools.core import (
    ArgumentValidator,
    MAX_TOOL_RESULT_BYTES,
    ResultValidator,
    ToolExecutionError,
    ToolProtocolError,
    ToolSpec,
    UnknownToolError,
    serialize_tool_result,
    validate_tool_arguments,
)
from aiweekend_target.agent_protocol import strict_json


class MCPSession(Protocol):
    async def initialize(self) -> object: ...

    async def list_tools(self) -> object: ...

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object: ...


def _attribute(value: object, snake: str, camel: str, default: object = None) -> object:
    if hasattr(value, snake):
        return getattr(value, snake)
    return getattr(value, camel, default)


class MCPToolProvider:
    """Expose only named MCP tools and normalize their bounded JSON results.

    The caller owns the MCP session lifetime.  Discovery is lazy and cached, so
    the same immutable surface is used for the whole autonomous run.
    """

    def __init__(
        self,
        session: MCPSession,
        *,
        allowlist: Collection[str],
        argument_validators: Mapping[str, ArgumentValidator] | None = None,
        result_validators: Mapping[str, ResultValidator] | None = None,
        max_result_bytes: int = MAX_TOOL_RESULT_BYTES,
    ) -> None:
        if type(max_result_bytes) is not int or max_result_bytes < 1:
            raise ValueError("max_result_bytes must be a positive integer")
        names = tuple(allowlist)
        if any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
            raise ToolProtocolError("MCP allowlist is invalid")
        self._session = session
        self._allowlist = frozenset(names)
        self._argument_validators = dict(argument_validators or {})
        self._result_validators = dict(result_validators or {})
        if not set(self._argument_validators) <= self._allowlist or not set(self._result_validators) <= self._allowlist:
            raise ToolProtocolError("MCP validators must target allowlisted tools")
        self._max_result_bytes = max_result_bytes
        self._specs: tuple[ToolSpec, ...] | None = None

    async def list_tools(self) -> Sequence[ToolSpec]:
        if self._specs is not None:
            return self._specs
        if not self._allowlist:
            self._specs = ()
            return self._specs
        try:
            await self._session.initialize()
            listing = await self._session.list_tools()
        except Exception as error:
            raise ToolExecutionError("MCP tool discovery failed") from error
        if (
            getattr(listing, "result_type", "complete") != "complete"
            or _attribute(listing, "next_cursor", "nextCursor") is not None
        ):
            raise ToolProtocolError("MCP tool discovery is incomplete")
        discovered: dict[str, ToolSpec] = {}
        for tool in getattr(listing, "tools", ()):
            name = getattr(tool, "name", None)
            if name not in self._allowlist:
                continue
            if name in discovered:
                raise ToolProtocolError(f"MCP advertised a duplicate tool: {name}")
            description = getattr(tool, "description", None)
            if description is None:
                description = ""
            schema = _attribute(tool, "input_schema", "inputSchema")
            discovered[name] = ToolSpec(name, description, schema)
        missing = self._allowlist.difference(discovered)
        if missing:
            raise ToolProtocolError("MCP did not advertise every allowlisted tool")
        self._specs = tuple(discovered[name] for name in sorted(discovered))
        return self._specs

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        specs = await self.list_tools()
        if name not in {spec.name for spec in specs}:
            raise UnknownToolError(f"tool is not allowlisted: {name}")
        checked = validate_tool_arguments(arguments)
        validator = self._argument_validators.get(name)
        if validator is not None:
            try:
                checked = validate_tool_arguments(validator(checked))
            except ToolProtocolError:
                raise
            except Exception as error:
                raise ToolProtocolError(f"tool arguments were rejected: {name}") from error
        try:
            result = await self._session.call_tool(name, checked)
        except Exception as error:
            raise ToolExecutionError(f"MCP tool execution failed: {name}") from error
        if (
            getattr(result, "result_type", "complete") != "complete"
            or _attribute(result, "is_error", "isError", False) is True
        ):
            raise ToolExecutionError(f"MCP tool execution failed: {name}")
        value = _attribute(result, "structured_content", "structuredContent")
        if value is None:
            content: list[dict[str, str]] = []
            for block in getattr(result, "content", ()):
                if getattr(block, "type", None) != "text" or not isinstance(getattr(block, "text", None), str):
                    raise ToolProtocolError("MCP returned unsupported unstructured content")
                content.append({"type": "text", "text": block.text})
            value = {"content": content}
        validator = self._result_validators.get(name)
        if validator is not None:
            try:
                value = validator(value)
            except ToolProtocolError:
                raise
            except Exception as error:
                raise ToolProtocolError(f"tool result was rejected: {name}") from error
        return strict_json(serialize_tool_result(value, maximum=self._max_result_bytes))


__all__ = ["MCPSession", "MCPToolProvider"]
