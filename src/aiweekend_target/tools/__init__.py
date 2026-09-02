"""Trusted tool surfaces for autonomous agents."""

from aiweekend_target.tools.core import (
    ArgumentValidator,
    MAX_TOOL_ARGUMENT_BYTES,
    MAX_TOOL_RESULT_BYTES,
    ResultValidator,
    ToolError,
    ToolExecutionError,
    ToolHandler,
    ToolProtocolError,
    ToolProvider,
    ToolRegistry,
    ToolSpec,
    UnknownToolError,
    serialize_tool_result,
    validate_tool_arguments,
    validate_tool_arguments_against_schema,
    validate_tool_json_value,
)
from aiweekend_target.tools.mcp import MCPSession, MCPToolProvider
from aiweekend_target.tools.router import CompositeToolProvider


__all__ = [
    "ArgumentValidator",
    "CompositeToolProvider",
    "MAX_TOOL_ARGUMENT_BYTES",
    "MAX_TOOL_RESULT_BYTES",
    "MCPSession",
    "MCPToolProvider",
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
