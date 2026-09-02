"""Universal bounded, provider-neutral autonomous function-calling engine.

Unlike the legacy lab runner, this runner does not force a tool call or a fixed
number of model turns.  The selected model may answer immediately or request
any advertised tool.  The trusted host still owns discovery, execution,
budgets, and the final evaluation.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeAlias

from aiweekend_target.agent_protocol import strict_json
from aiweekend_target.core.contracts import (
    Boundary,
    BoundaryContext,
    ControlAction,
    ModelDescriptor,
    ModelInvocation,
    ModelProvider,
    checked_identifier,
    detached_json,
)
from aiweekend_target.core.pipeline import BoundaryPipeline, BoundaryPipelineError
from aiweekend_target.tools import (
    MAX_TOOL_ARGUMENT_BYTES,
    MAX_TOOL_RESULT_BYTES,
    ToolProvider,
    ToolProtocolError,
    ToolSpec,
    UnknownToolError,
    serialize_tool_result,
    validate_tool_arguments,
    validate_tool_arguments_against_schema,
)


MAX_MODEL_RESPONSE_BYTES = 256 * 1024
MAX_ANSWER_BYTES = 64 * 1024
_CALL_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class AutonomousAgentError(RuntimeError):
    """Base error carrying a stable failing boundary and reason."""

    def __init__(self, reason: str, stage: str) -> None:
        self.reason = reason
        self.stage = stage
        super().__init__(reason)


class AgentProtocolError(AutonomousAgentError):
    """The model returned an invalid assistant turn."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason, "llm")


class AgentBudgetExceeded(AutonomousAgentError):
    """A deterministic run budget was exhausted."""

    def __init__(self, budget: str) -> None:
        self.budget = budget
        super().__init__(f"agent budget exceeded: {budget}", "budget")


class AgentControlBlocked(AutonomousAgentError):
    """A shared host policy prevented delivery or aborted the run."""

    def __init__(self, boundary: Boundary, reason: str) -> None:
        self.boundary = boundary
        super().__init__(reason, "control")


class ModelInvocationError(AutonomousAgentError):
    """The configured model client failed at its transport boundary."""

    def __init__(self) -> None:
        super().__init__("model invocation failed", "llm")


@dataclass(frozen=True)
class AgentLimits:
    """Hard limits enforced by the runner independently of the model."""

    max_turns: int = 8
    max_tool_calls: int = 8
    max_identical_tool_calls: int = 3
    max_wall_seconds: float = 180.0
    max_request_bytes: int = 512 * 1024
    max_response_bytes: int = MAX_MODEL_RESPONSE_BYTES
    max_answer_bytes: int = MAX_ANSWER_BYTES
    max_output_tokens: int = 2_048
    max_tool_result_bytes: int = MAX_TOOL_RESULT_BYTES
    max_total_tool_result_bytes: int = 256 * 1024
    max_reported_tokens: int | None = None

    def __post_init__(self) -> None:
        positive_integers = (
            self.max_turns,
            self.max_identical_tool_calls,
            self.max_request_bytes,
            self.max_response_bytes,
            self.max_answer_bytes,
            self.max_output_tokens,
            self.max_tool_result_bytes,
            self.max_total_tool_result_bytes,
        )
        if any(type(value) is not int or value < 1 for value in positive_integers):
            raise ValueError("positive agent limits must be positive integers")
        if type(self.max_tool_calls) is not int or self.max_tool_calls < 0:
            raise ValueError("max_tool_calls must be a non-negative integer")
        if (
            isinstance(self.max_wall_seconds, bool)
            or not isinstance(self.max_wall_seconds, int | float)
            or self.max_wall_seconds <= 0
        ):
            raise ValueError("max_wall_seconds must be positive")
        if self.max_reported_tokens is not None and (
            type(self.max_reported_tokens) is not int or self.max_reported_tokens < 1
        ):
            raise ValueError("max_reported_tokens must be a positive integer")


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, object]
    assistant_call: dict[str, object]


@dataclass(frozen=True)
class ToolCallRecord:
    """Private trusted evidence; unlike public trace events it retains data."""

    call_id: str
    name: str
    arguments: dict[str, object]
    decision: str
    executed: bool
    result: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not _CALL_ID.fullmatch(self.call_id):
            raise ValueError("tool evidence call id is invalid")
        if not isinstance(self.name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.name):
            raise ValueError("tool evidence name is invalid")
        if self.decision not in {"allow", "block"}:
            raise ValueError("tool evidence decision is invalid")
        if type(self.executed) is not bool or self.decision == "block" and self.executed:
            raise ValueError("tool evidence execution state is invalid")
        object.__setattr__(self, "arguments", validate_tool_arguments(self.arguments))
        if self.result is not None:
            detached, _ = _json_document(
                self.result,
                maximum=MAX_TOOL_RESULT_BYTES,
                label="tool_evidence_result_bytes",
            )
            object.__setattr__(self, "result", detached)


@dataclass(frozen=True)
class AutonomousResult:
    """Successful model output plus facts used by an independent evaluator."""

    answer: str
    turns: int
    tool_calls: int
    reported_tokens: int | None
    usage_complete: bool
    messages: tuple[dict[str, object], ...]
    tool_records: tuple[ToolCallRecord, ...]


ModelComplete: TypeAlias = Callable[[dict[str, object]], Awaitable[Mapping[str, object]]]
ModelProviderComplete: TypeAlias = Callable[
    [dict[str, object], ModelInvocation], Awaitable[Mapping[str, object]]
]
EventSink: TypeAlias = Callable[[dict[str, object]], None]
EvidenceSink: TypeAlias = Callable[[ToolCallRecord], None]


class CallbackModelProvider:
    """Adapt an existing completion transport to the shared model contract."""

    def __init__(self, descriptor: ModelDescriptor, complete: ModelComplete) -> None:
        if not callable(complete):
            raise ValueError("model completion callback must be callable")
        self._descriptor = descriptor
        self._complete = complete

    async def describe(self) -> ModelDescriptor:
        return self._descriptor

    async def complete(
        self, request: dict[str, object], invocation: ModelInvocation
    ) -> Mapping[str, object]:
        return await self._complete(request)


def _json_document(value: object, *, maximum: int, label: str) -> tuple[object, int]:
    try:
        document = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        size = len(document.encode("utf-8"))
        if size > maximum:
            raise AgentBudgetExceeded(label)
        return strict_json(document), size
    except AgentBudgetExceeded:
        raise
    except (TypeError, ValueError, UnicodeError) as error:
        raise AgentProtocolError(f"{label} must be strict JSON") from error


def _bounded_text(value: object, maximum: int, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise AgentProtocolError(f"{label} must be text")
    if len(value.encode("utf-8")) > maximum:
        raise AgentBudgetExceeded(label)
    return value


def _reported_tokens(document: Mapping[str, object]) -> int | None:
    usage = document.get("usage")
    if usage is None:
        return None
    if not isinstance(usage, Mapping):
        return None
    total = usage.get("total_tokens")
    return total if type(total) is int and total >= 0 else None


def _parse_tool_call(value: object) -> ToolCall:
    if not isinstance(value, dict) or set(value) != {"id", "type", "function"}:
        raise AgentProtocolError("tool call has invalid fields")
    call_id = value.get("id")
    function = value.get("function")
    if (
        value.get("type") != "function"
        or not isinstance(call_id, str)
        or not _CALL_ID.fullmatch(call_id)
        or not isinstance(function, dict)
        or set(function) != {"name", "arguments"}
    ):
        raise AgentProtocolError("tool call is invalid")
    name = function.get("name")
    raw_arguments = function.get("arguments")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise AgentProtocolError("tool call name is invalid")
    if (
        not isinstance(raw_arguments, str)
        or not raw_arguments
        or len(raw_arguments.encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES
    ):
        raise AgentProtocolError("tool call arguments must be bounded JSON")
    try:
        arguments = validate_tool_arguments(strict_json(raw_arguments))
    except (ToolProtocolError, ValueError, UnicodeError) as error:
        raise AgentProtocolError("tool call arguments must be a strict JSON object") from error
    canonical_arguments = json.dumps(arguments, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    assistant_call = {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": canonical_arguments},
    }
    return ToolCall(call_id, name, arguments, assistant_call)


def _parse_turn(
    response: Mapping[str, object],
    limits: AgentLimits,
) -> tuple[dict[str, object], tuple[ToolCall, ...], str | None, int | None]:
    detached, _ = _json_document(response, maximum=limits.max_response_bytes, label="model_response_bytes")
    if not isinstance(detached, dict):
        raise AgentProtocolError("model response must be a JSON object")
    choices = detached.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise AgentProtocolError("model response must contain exactly one choice")
    message = choices[0].get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise AgentProtocolError("model response has no assistant message")
    calls_value = message.get("tool_calls")
    if calls_value in (None, []):
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            refusal = message.get("refusal")
            content = refusal if isinstance(refusal, str) and refusal.strip() else content
        answer = _bounded_text(content, limits.max_answer_bytes, "answer_bytes").strip()
        return {"role": "assistant", "content": answer}, (), answer, _reported_tokens(detached)
    if not isinstance(calls_value, list) or not calls_value:
        raise AgentProtocolError("assistant tool_calls must be a non-empty list")
    content_value = message.get("content")
    content = None
    if content_value is not None:
        content = _bounded_text(content_value, limits.max_answer_bytes, "assistant_content_bytes", allow_empty=True)
    calls = tuple(_parse_tool_call(value) for value in calls_value)
    if len({call.call_id for call in calls}) != len(calls):
        raise AgentProtocolError("assistant reused a tool call id")
    assistant = {
        "role": "assistant",
        "content": content,
        "tool_calls": [call.assistant_call for call in calls],
    }
    return assistant, calls, None, _reported_tokens(detached)


def _checked_specs(specs: Sequence[ToolSpec]) -> tuple[ToolSpec, ...]:
    result = tuple(specs)
    if any(not isinstance(spec, ToolSpec) for spec in result):
        raise ToolProtocolError("tool provider returned invalid metadata")
    if len({spec.name for spec in result}) != len(result):
        raise ToolProtocolError("tool provider returned duplicate names")
    return result


def _detached_messages(messages: Sequence[Mapping[str, object]]) -> tuple[dict[str, object], ...]:
    result: list[dict[str, object]] = []
    allowed_roles = {"system", "user", "assistant", "tool"}
    for index, message in enumerate(messages):
        detached = strict_json(
            json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        )
        if not isinstance(detached, dict) or detached.get("role") not in allowed_roles:
            raise AgentProtocolError("agent history contains an invalid message")
        if detached["role"] == "system" and index != 0:
            raise AgentProtocolError("system history is only valid at position zero")
        result.append(detached)
    return tuple(result)


def _emit(sink: EventSink | None, event_type: str, **facts: object) -> None:
    if sink is not None:
        sink({"schema": 2, "type": event_type, **facts})


async def _cross(
    pipeline: BoundaryPipeline | None,
    boundary: Boundary,
    payload: object,
    *,
    run_id: str,
    user_turn: int,
    model_turn: int,
    correlation_id: str | None = None,
    tool_name: str | None = None,
    provenance: Sequence[str] = (),
) -> tuple[ControlAction, object | None, str, tuple[str, ...]]:
    if pipeline is None:
        return (
            ControlAction.ALLOW,
            detached_json(payload),
            "pipeline.absent",
            tuple(dict.fromkeys(provenance)),
        )
    try:
        outcome = await pipeline.process(
            BoundaryContext(
                boundary=boundary,
                payload=payload,
                run_id=run_id,
                user_turn=user_turn,
                model_turn=model_turn,
                correlation_id=correlation_id,
                tool_name=tool_name,
                provenance=tuple(provenance),
            )
        )
    except BoundaryPipelineError as error:
        raise AgentControlBlocked(boundary, "boundary_pipeline_failure") from error
    return (
        outcome.decision.action,
        outcome.delivered_payload,
        outcome.decision.reason,
        tuple(dict.fromkeys(outcome.context.provenance)),
    )


def _require_delivery(
    action: ControlAction,
    payload: object | None,
    boundary: Boundary,
    reason: str,
) -> object:
    if action in {ControlAction.BLOCK, ControlAction.ABORT} or payload is None:
        raise AgentControlBlocked(boundary, reason)
    return payload


def _record(
    records: list[ToolCallRecord],
    sink: EvidenceSink | None,
    call: ToolCall,
    *,
    decision: str,
    executed: bool,
    result: object | None = None,
) -> None:
    record = ToolCallRecord(
        call.call_id,
        call.name,
        call.arguments,
        decision,
        executed,
        result,
    )
    records.append(record)
    if sink is not None:
        sink(record)


async def _run_loop(
    task: str,
    *,
    model: str,
    complete: ModelComplete | None,
    provider_complete: ModelProviderComplete | None,
    tools: ToolProvider,
    limits: AgentLimits,
    system_prompt: str | None,
    history: Sequence[Mapping[str, object]],
    pipeline: BoundaryPipeline | None,
    run_id: str,
    user_turn: int,
    opaque_call_ids: bool,
    event_sink: EventSink | None,
    evidence_sink: EvidenceSink | None,
) -> AutonomousResult:
    task = _bounded_text(task, limits.max_request_bytes, "task_bytes").strip()
    if (complete is None) == (provider_complete is None):
        raise ValueError("exactly one model completion transport is required")
    if not isinstance(model, str) or not model or len(model.encode("utf-8")) > 512:
        raise ValueError("model must be a bounded non-empty string")
    action, delivered, reason, input_provenance = await _cross(
        pipeline,
        Boundary.INPUT,
        task,
        run_id=run_id,
        user_turn=user_turn,
        model_turn=0,
        provenance=("source.user",),
    )
    task = _bounded_text(
        _require_delivery(action, delivered, Boundary.INPUT, reason),
        limits.max_request_bytes,
        "task_bytes",
    ).strip()
    conversation_provenance = list(input_provenance)
    messages = list(_detached_messages(history))
    if not messages and system_prompt is not None:
        messages.append(
            {
                "role": "system",
                "content": _bounded_text(
                    system_prompt,
                    limits.max_request_bytes,
                    "system_prompt_bytes",
                ),
            }
        )
    messages.append({"role": "user", "content": task})
    specs = _checked_specs(await tools.list_tools())
    if limits.max_tool_calls == 0:
        specs = ()
    specs_by_name = {spec.name: spec for spec in specs}
    names = set(specs_by_name)
    public_tools = [spec.as_openai_tool() for spec in specs]
    total_calls = 0
    total_result_bytes = 0
    token_total = 0
    usage_complete = True
    seen_call_ids: set[str] = set()
    repeated_calls: Counter[str] = Counter()
    tool_records: list[ToolCallRecord] = []
    public_call_ids: dict[str, str] = {}

    def public_call_id(call_id: str) -> str:
        if not opaque_call_ids:
            return call_id
        if call_id not in public_call_ids:
            public_call_ids[call_id] = f"call-{len(public_call_ids) + 1:04d}"
        return public_call_ids[call_id]

    for turn in range(1, limits.max_turns + 1):
        model_request_id = f"model-{user_turn:04d}-{turn:04d}"
        request: dict[str, object] = {
            "model": model,
            "messages": messages,
            "max_tokens": limits.max_output_tokens,
            "stream": False,
        }
        if public_tools:
            request.update(
                {
                    "tools": public_tools,
                    "tool_choice": "auto",
                    "parallel_tool_calls": False,
                }
            )
        action, delivered, reason, request_provenance = await _cross(
            pipeline,
            Boundary.MODEL_REQUEST,
            request,
            run_id=run_id,
            user_turn=user_turn,
            model_turn=turn,
            correlation_id=model_request_id,
            provenance=tuple(dict.fromkeys(("source.host", *conversation_provenance))),
        )
        controlled_request = _require_delivery(
            action, delivered, Boundary.MODEL_REQUEST, reason
        )
        if not isinstance(controlled_request, dict):
            raise AgentProtocolError("controlled model request must remain an object")
        if controlled_request.get("model") != model:
            raise AgentProtocolError("control pipeline cannot replace the selected model")
        if public_tools and (
            controlled_request.get("tool_choice") != "auto"
            or controlled_request.get("parallel_tool_calls") is not False
            or controlled_request.get("tools") != public_tools
        ):
            raise AgentProtocolError("control pipeline changed the host tool contract")
        if not public_tools and (
            "tools" in controlled_request or "tool_choice" in controlled_request
        ):
            raise AgentProtocolError("empty tool catalogs cannot create tool fields")
        checked_request, request_bytes = _json_document(
            controlled_request,
            maximum=limits.max_request_bytes,
            label="request_bytes",
        )
        if not isinstance(checked_request, dict):
            raise AgentProtocolError("model request must be a JSON object")
        _emit(
            event_sink,
            "llm_request",
            turn=turn,
            model_request_id=model_request_id,
            model=model,
            message_count=len(messages),
            tool_count=len(public_tools),
            tool_choice="auto" if public_tools else "none",
            request_bytes=request_bytes,
        )
        try:
            if provider_complete is not None:
                response = await provider_complete(
                    checked_request,
                    ModelInvocation(
                        run_id=run_id,
                        request_id=model_request_id,
                        user_turn=user_turn,
                        model_turn=turn,
                    ),
                )
            else:
                assert complete is not None
                response = await complete(checked_request)
        except AutonomousAgentError:
            raise
        except Exception as error:
            raise ModelInvocationError() from error
        if not isinstance(response, Mapping):
            raise AgentProtocolError("model client returned a non-object response")
        action, delivered, reason, response_provenance = await _cross(
            pipeline,
            Boundary.MODEL_OUTPUT,
            dict(response),
            run_id=run_id,
            user_turn=user_turn,
            model_turn=turn,
            correlation_id=model_request_id,
            provenance=tuple(dict.fromkeys(("source.model", *request_provenance))),
        )
        controlled_response = _require_delivery(
            action, delivered, Boundary.MODEL_OUTPUT, reason
        )
        if not isinstance(controlled_response, dict):
            raise AgentProtocolError("controlled model response must remain an object")
        assistant, calls, answer, turn_tokens = _parse_turn(controlled_response, limits)
        if turn_tokens is None:
            usage_complete = False
        else:
            token_total += turn_tokens
        _emit(
            event_sink,
            "llm_response",
            turn=turn,
            model_request_id=model_request_id,
            model=model,
            outcome="final" if answer is not None else "tool_calls",
            tool_call_count=len(calls),
            reported_tokens=turn_tokens,
        )
        for call in calls:
            argument_bytes = len(
                json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            _emit(
                event_sink,
                "tool_call_proposed",
                turn=turn,
                call_id=public_call_id(call.call_id),
                tool=call.name,
                argument_bytes=argument_bytes,
            )
        messages.append(assistant)
        if limits.max_reported_tokens is not None and token_total > limits.max_reported_tokens:
            for call in calls:
                _emit(
                    event_sink,
                    "policy_decision",
                    turn=turn,
                    call_id=public_call_id(call.call_id),
                    tool=call.name,
                    outcome="block",
                    reason="budget",
                    budget="reported_tokens",
                )
                _record(tool_records, evidence_sink, call, decision="block", executed=False)
            raise AgentBudgetExceeded("reported_tokens")
        if answer is not None:
            action, delivered, reason, _ = await _cross(
                pipeline,
                Boundary.FINAL_OUTPUT,
                answer,
                run_id=run_id,
                user_turn=user_turn,
                model_turn=turn,
                provenance=tuple(dict.fromkeys(("source.model", *response_provenance))),
            )
            answer = _bounded_text(
                _require_delivery(action, delivered, Boundary.FINAL_OUTPUT, reason),
                limits.max_answer_bytes,
                "answer_bytes",
            ).strip()
            messages[-1] = {"role": "assistant", "content": answer}
            _emit(
                event_sink,
                "final_answer",
                turn=turn,
                answer_bytes=len(answer.encode("utf-8")),
            )
            return AutonomousResult(
                answer=answer,
                turns=turn,
                tool_calls=total_calls,
                reported_tokens=token_total if usage_complete else None,
                usage_complete=usage_complete,
                messages=_detached_messages(messages),
                tool_records=tuple(tool_records),
            )

        if turn == limits.max_turns:
            for call in calls:
                _emit(
                    event_sink,
                    "policy_decision",
                    turn=turn,
                    call_id=public_call_id(call.call_id),
                    tool=call.name,
                    outcome="block",
                    reason="budget",
                    budget="turns",
                )
                _record(tool_records, evidence_sink, call, decision="block", executed=False)
            raise AgentBudgetExceeded("turns")
        if total_calls + len(calls) > limits.max_tool_calls:
            for call in calls:
                _emit(
                    event_sink,
                    "policy_decision",
                    turn=turn,
                    call_id=public_call_id(call.call_id),
                    tool=call.name,
                    outcome="block",
                    reason="budget",
                    budget="tool_calls",
                )
                _record(tool_records, evidence_sink, call, decision="block", executed=False)
            raise AgentBudgetExceeded("tool_calls")
        duplicate_ids = {call.call_id for call in calls if call.call_id in seen_call_ids}
        unknown_names = {call.name for call in calls if call.name not in names}
        invalid_arguments: set[str] = set()
        repeat_violations: set[str] = set()
        pending_repeats: Counter[str] = Counter()
        for call in calls:
            spec = specs_by_name.get(call.name)
            if spec is None:
                continue
            try:
                validate_tool_arguments_against_schema(
                    call.arguments, spec.input_schema
                )
            except ToolProtocolError:
                invalid_arguments.add(call.call_id)
            fingerprint = (
                f"{call.name}:"
                + json.dumps(call.arguments, sort_keys=True, separators=(",", ":"))
            )
            pending_repeats[fingerprint] += 1
            if (
                repeated_calls[fingerprint] + pending_repeats[fingerprint]
                > limits.max_identical_tool_calls
            ):
                repeat_violations.add(call.call_id)
        if duplicate_ids or unknown_names or invalid_arguments or repeat_violations:
            for call in calls:
                if call.call_id in duplicate_ids:
                    reason = "duplicate_call_id"
                elif call.name in unknown_names:
                    reason = "unknown_tool"
                elif call.call_id in invalid_arguments:
                    reason = "invalid_arguments"
                elif call.call_id in repeat_violations:
                    reason = "repeated_tool_call"
                else:
                    reason = "batch_rejected"
                _emit(
                    event_sink,
                    "policy_decision",
                    turn=turn,
                    call_id=public_call_id(call.call_id),
                    tool=call.name,
                    outcome="block",
                    reason=reason,
                )
                if call.call_id not in duplicate_ids:
                    _record(tool_records, evidence_sink, call, decision="block", executed=False)
            if duplicate_ids:
                raise AgentProtocolError("assistant reused a tool call id")
            if invalid_arguments:
                raise AgentProtocolError(
                    "tool arguments do not match the advertised input schema"
                )
            if repeat_violations:
                raise AgentBudgetExceeded("repeated_tool_call")
            unknown = next(call.name for call in calls if call.name in unknown_names)
            raise UnknownToolError(f"tool is not allowlisted: {unknown}")
        seen_call_ids.update(call.call_id for call in calls)

        for call in calls:
            correlation_id = public_call_id(call.call_id)
            action, controlled_call, control_reason, call_provenance = await _cross(
                pipeline,
                Boundary.TOOL_CALL,
                {"name": call.name, "arguments": call.arguments},
                run_id=run_id,
                user_turn=user_turn,
                model_turn=turn,
                correlation_id=correlation_id,
                tool_name=call.name,
                provenance=tuple(dict.fromkeys(("source.model", *response_provenance))),
            )
            if action is ControlAction.ABORT:
                _record(tool_records, evidence_sink, call, decision="block", executed=False)
                raise AgentControlBlocked(Boundary.TOOL_CALL, control_reason)
            if action is ControlAction.BLOCK:
                _record(tool_records, evidence_sink, call, decision="block", executed=False)
                _emit(
                    event_sink,
                    "policy_decision",
                    turn=turn,
                    call_id=correlation_id,
                    tool=call.name,
                    outcome="block",
                    reason=control_reason,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": '{"error":"blocked_by_host_policy"}',
                    }
                )
                continue
            if not isinstance(controlled_call, dict) or set(controlled_call) != {
                "name",
                "arguments",
            }:
                raise AgentProtocolError("controlled tool call is invalid")
            controlled_name = controlled_call.get("name")
            controlled_arguments = controlled_call.get("arguments")
            if not isinstance(controlled_name, str) or not isinstance(
                controlled_arguments, dict
            ):
                raise AgentProtocolError("controlled tool call is invalid")
            controlled_spec = specs_by_name.get(controlled_name)
            if controlled_spec is None:
                raise UnknownToolError(f"tool is not allowlisted: {controlled_name}")
            controlled_arguments = validate_tool_arguments_against_schema(
                controlled_arguments, controlled_spec.input_schema
            )
            execution_call = ToolCall(
                call.call_id,
                controlled_name,
                controlled_arguments,
                call.assistant_call,
            )
            fingerprint = (
                f"{controlled_name}:"
                + json.dumps(controlled_arguments, sort_keys=True, separators=(",", ":"))
            )
            repeated_calls[fingerprint] += 1
            _emit(
                event_sink,
                "policy_decision",
                turn=turn,
                call_id=correlation_id,
                tool=controlled_name,
                outcome="allow",
                reason=control_reason,
            )
            _emit(
                event_sink,
                "tool_execution_started",
                turn=turn,
                call_id=correlation_id,
                tool=controlled_name,
            )
            try:
                result = await tools.call_tool(controlled_name, controlled_arguments)
            except Exception:
                _record(
                    tool_records,
                    evidence_sink,
                    execution_call,
                    decision="allow",
                    executed=False,
                )
                _emit(
                    event_sink,
                    "tool_execution_failed",
                    turn=turn,
                    call_id=correlation_id,
                    tool=controlled_name,
                )
                raise
            try:
                result_document = serialize_tool_result(
                    result, maximum=limits.max_tool_result_bytes
                )
                trusted_result = strict_json(result_document)
            except Exception:
                # The side effect already happened even if its return value violates
                # the protocol, so private evidence must retain that execution state.
                _record(
                    tool_records,
                    evidence_sink,
                    execution_call,
                    decision="allow",
                    executed=True,
                )
                raise
            _record(
                tool_records,
                evidence_sink,
                execution_call,
                decision="allow",
                executed=True,
                result=trusted_result,
            )
            result_bytes = len(result_document.encode("utf-8"))
            total_result_bytes += result_bytes
            if total_result_bytes > limits.max_total_tool_result_bytes:
                raise AgentBudgetExceeded("total_tool_result_bytes")
            action, delivered_result, result_reason, result_provenance = await _cross(
                pipeline,
                Boundary.TOOL_RESULT,
                trusted_result,
                run_id=run_id,
                user_turn=user_turn,
                model_turn=turn,
                correlation_id=correlation_id,
                tool_name=controlled_name,
                provenance=tuple(dict.fromkeys(("source.tool", *call_provenance))),
            )
            if action is ControlAction.ABORT:
                raise AgentControlBlocked(Boundary.TOOL_RESULT, result_reason)
            if action is ControlAction.BLOCK:
                result_document = '{"error":"tool_result_blocked_by_host_policy"}'
                delivery = "blocked"
            else:
                result_document = serialize_tool_result(
                    delivered_result, maximum=limits.max_tool_result_bytes
                )
                delivery = (
                    "replaced" if action is ControlAction.REPLACE else "delivered"
                )
            conversation_provenance.extend(
                item for item in result_provenance if item not in conversation_provenance
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "content": result_document,
                }
            )
            total_calls += 1
            _emit(
                event_sink,
                "tool_result",
                turn=turn,
                call_id=correlation_id,
                tool=controlled_name,
                result_bytes=len(result_document.encode("utf-8")),
                delivery=delivery,
            )

    raise AgentBudgetExceeded("turns")


async def run_autonomous(
    task: str,
    *,
    model: str,
    complete: ModelComplete | None = None,
    provider_complete: ModelProviderComplete | None = None,
    tools: ToolProvider,
    limits: AgentLimits = AgentLimits(),
    system_prompt: str | None = None,
    history: Sequence[Mapping[str, object]] = (),
    pipeline: BoundaryPipeline | None = None,
    run_id: str = "agent-run",
    user_turn: int = 1,
    opaque_call_ids: bool = False,
    event_sink: EventSink | None = None,
    evidence_sink: EvidenceSink | None = None,
) -> AutonomousResult:
    """Run one autonomous agent session without retries or hidden tool calls."""
    checked_identifier(run_id, "run id")
    if type(user_turn) is not int or user_turn < 1:
        raise ValueError("user turn must be positive")
    try:
        async with asyncio.timeout(limits.max_wall_seconds):
            return await _run_loop(
                task,
                model=model,
                complete=complete,
                provider_complete=provider_complete,
                tools=tools,
                limits=limits,
                system_prompt=system_prompt,
                history=history,
                pipeline=pipeline,
                run_id=run_id,
                user_turn=user_turn,
                opaque_call_ids=opaque_call_ids,
                event_sink=event_sink,
                evidence_sink=evidence_sink,
            )
    except TimeoutError as error:
        raise AgentBudgetExceeded("wall_time") from error


class AgentSession:
    """Persistent conversation over the exact same bounded engine as batch."""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        tools: ToolProvider,
        pipeline: BoundaryPipeline,
        limits: AgentLimits = AgentLimits(),
        system_prompt: str | None = None,
        run_id: str = "agent-session",
        event_sink: EventSink | None = None,
        evidence_sink: EvidenceSink | None = None,
    ) -> None:
        checked_identifier(run_id, "run id")
        self._provider = provider
        self._tools = tools
        self._pipeline = pipeline
        self._limits = limits
        self._system_prompt = system_prompt
        self._run_id = run_id
        self._event_sink = event_sink
        self._evidence_sink = evidence_sink
        self._messages: tuple[dict[str, object], ...] = ()
        self._user_turn = 0

    @property
    def messages(self) -> tuple[dict[str, object], ...]:
        return _detached_messages(self._messages)

    async def run_turn(self, text: str) -> AutonomousResult:
        descriptor = await self._provider.describe()
        required = {"chat_completions"}
        if await self._tools.list_tools():
            required.add("tool_calls")
        missing = required - descriptor.capabilities
        if missing:
            raise ValueError(
                f"selected model lacks capabilities: {', '.join(sorted(missing))}"
            )
        self._user_turn += 1
        result = await run_autonomous(
            text,
            model=descriptor.id,
            provider_complete=self._provider.complete,
            tools=self._tools,
            limits=self._limits,
            system_prompt=self._system_prompt,
            history=self._messages,
            pipeline=self._pipeline,
            run_id=self._run_id,
            user_turn=self._user_turn,
            opaque_call_ids=True,
            event_sink=self._event_sink,
            evidence_sink=self._evidence_sink,
        )
        self._messages = result.messages
        return result


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
