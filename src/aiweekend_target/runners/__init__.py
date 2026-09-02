"""Agent runner implementations.

The original :mod:`aiweekend_target.agent` module remains the fixed legacy
runner.  New scenarios can opt into the autonomous runner explicitly.
"""

from aiweekend_target.runners.autonomous import (
    AgentBudgetExceeded,
    AgentControlBlocked,
    AgentLimits,
    AgentProtocolError,
    AgentSession,
    AutonomousAgentError,
    AutonomousResult,
    CallbackModelProvider,
    EvidenceSink,
    EventSink,
    ModelComplete,
    ModelInvocationError,
    ToolCall,
    ToolCallRecord,
    run_autonomous,
)


__all__ = [
    "AgentBudgetExceeded",
    "AgentControlBlocked",
    "AgentLimits",
    "AgentProtocolError",
    "AgentSession",
    "AutonomousAgentError",
    "AutonomousResult",
    "CallbackModelProvider",
    "EvidenceSink",
    "EventSink",
    "ModelComplete",
    "ModelInvocationError",
    "ToolCall",
    "ToolCallRecord",
    "run_autonomous",
]
