"""Tool visibility is treated as permission; argument-level grants are not checked."""


def visible_grant(event, context) -> bool:
    return event.tool_name in context.visible_tools
