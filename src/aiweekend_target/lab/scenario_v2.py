"""Strict declarative scenario manifests for the autonomous v2 runner.

Scenario loading and private-oracle loading are intentionally separate.  The
agent-facing process should receive only :class:`ScenarioManifest`; an evaluator
process can load :class:`PrivateOracle` from a mount that is not visible to the
agent or any model-facing tool.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from aiweekend_target.agent_protocol import strict_json
from aiweekend_target.errors import ErrorCode, TargetError
from aiweekend_target.lab.trace_v2 import Taint


MAX_DESCRIPTOR_BYTES = 32_768
MAX_TASK_BYTES = 65_536
MAX_PAYLOAD_BYTES = 65_536
MAX_ORACLE_BYTES = 32_768
_SCENARIO_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_PATH_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HOOKS = {
    "task",
    "before_llm_request",
    "tool_description",
    "rag_document",
    "tool_result",
    "memory_read",
    "before_final",
}
_OPERATIONS = {"append", "prepend", "replace"}
_CANARY_CHANNELS = {"final_answer", "tool_arguments"}
_CAPABILITIES = {"chat_completions", "tool_calls", "vision"}


@dataclass(frozen=True)
class RunBudget:
    max_turns: int
    max_tool_calls: int
    timeout_seconds: int
    max_output_tokens: int


@dataclass(frozen=True)
class InjectionRule:
    id: str
    hook: str
    operation: str
    target: str | None
    taint: Taint
    payload: str


@dataclass(frozen=True)
class ScenarioManifest:
    """Public, model-agnostic inputs used by a runner."""

    id: str
    task: str
    tools: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    budget: RunBudget
    injections: tuple[InjectionRule, ...]
    oracle_ref: str


@dataclass(frozen=True)
class CanaryOracle:
    id: str
    value: str


@dataclass(frozen=True)
class AnswerOracle:
    mode: str
    expected: tuple[str, ...]
    forbidden_substrings: tuple[str, ...]


@dataclass(frozen=True)
class ToolEvidenceRule:
    """Private evidence that must be present on an executed tool call."""

    name: str
    arguments_subset: dict[str, object] | None
    result_subset: dict[str, object] | None


@dataclass(frozen=True)
class ToolOracle:
    required: tuple[str, ...]
    optional: tuple[str, ...]
    forbidden: tuple[str, ...]
    evidence: tuple[ToolEvidenceRule, ...]


@dataclass(frozen=True)
class SecurityOracle:
    canaries: tuple[CanaryOracle, ...]
    forbidden_canary_channels: tuple[str, ...]
    forbidden_output_substrings: tuple[str, ...]


@dataclass(frozen=True)
class PrivateOracle:
    """Deterministic expectations that must never be exposed to the model."""

    scenario_id: str
    answer: AnswerOracle
    tools: ToolOracle
    security: SecurityOracle


@dataclass(frozen=True)
class InjectionApplication:
    value: str
    injection_ids: tuple[str, ...]
    taints: tuple[Taint, ...]


def load_scenario_manifest(directory: Path) -> ScenarioManifest:
    """Load a v2 scenario without reading its private oracle."""
    root = _checked_root(directory)
    value = _read_json(
        root, "scenario.json", MAX_DESCRIPTOR_BYTES, "scenario descriptor"
    )
    required_fields = {
        "schema",
        "id",
        "task_file",
        "tools",
        "required_capabilities",
        "budget",
        "injections",
        "oracle_file",
    }
    if not isinstance(value, dict) or set(value) != required_fields:
        raise _error("scenario descriptor has unexpected fields")
    scenario_id = _checked_scenario_id(value.get("id"))
    if value.get("schema") != 2 or isinstance(value.get("schema"), bool):
        raise _error("scenario descriptor has an invalid schema")
    task_ref = _checked_ref(value.get("task_file"), "task file")
    oracle_ref = _checked_ref(value.get("oracle_file"), "oracle file")
    if task_ref == oracle_ref:
        raise _error("private oracle cannot also be a public task")
    task = _read_text(root, task_ref, MAX_TASK_BYTES, "scenario task")
    if not task.strip():
        raise _error("scenario task is empty")
    tools = _checked_names(value.get("tools"), "scenario tools")
    capabilities = _checked_names(
        value.get("required_capabilities"), "required capabilities"
    )
    if not set(capabilities) <= _CAPABILITIES:
        raise _error("scenario requires an unsupported model capability")
    budget = _load_budget(value.get("budget"))
    injections = _load_injections(root, value.get("injections"), private_ref=oracle_ref)
    # Validate path containment and the file type, but deliberately do not read it.
    _resolve_file(root, oracle_ref, "private oracle")
    return ScenarioManifest(
        id=scenario_id,
        task=task,
        tools=tools,
        required_capabilities=capabilities,
        budget=budget,
        injections=injections,
        oracle_ref=oracle_ref,
    )


def load_private_oracle(directory: Path, scenario: ScenarioManifest) -> PrivateOracle:
    """Load and bind the private evaluator oracle to a public manifest."""
    if not isinstance(scenario, ScenarioManifest):
        raise _error("private oracle requires a validated scenario")
    root = _checked_root(directory)
    value = _read_json(root, scenario.oracle_ref, MAX_ORACLE_BYTES, "private oracle")
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "scenario_id",
        "answer",
        "tools",
        "security",
    }:
        raise _error("private oracle has unexpected fields")
    if (
        value.get("schema") != 1
        or isinstance(value.get("schema"), bool)
        or value.get("scenario_id") != scenario.id
    ):
        raise _error("private oracle does not match the scenario")
    answer = _load_answer_oracle(value.get("answer"))
    tools = _load_tool_oracle(value.get("tools"), scenario)
    security = _load_security_oracle(value.get("security"), scenario)
    return PrivateOracle(
        scenario_id=scenario.id, answer=answer, tools=tools, security=security
    )


def apply_text_injections(
    value: str,
    scenario: ScenarioManifest,
    hook: str,
    *,
    target: str | None = None,
) -> InjectionApplication:
    """Apply only the declared text transformations matching one trusted hook."""
    if not isinstance(value, str):
        raise _error("injection input must be text")
    if hook not in _HOOKS:
        raise _error("injection hook is invalid")
    if target is not None and (
        not isinstance(target, str) or not _NAME.fullmatch(target)
    ):
        raise _error("injection target is invalid")
    result = value
    applied: list[str] = []
    taints: list[Taint] = []
    for rule in scenario.injections:
        if rule.hook != hook or rule.target is not None and rule.target != target:
            continue
        if rule.operation == "append":
            result += rule.payload
        elif rule.operation == "prepend":
            result = rule.payload + result
        elif rule.operation == "replace":
            result = rule.payload
        else:  # The loader makes this unreachable, keeping execution closed by default.
            raise _error("injection operation is invalid")
        applied.append(rule.id)
        if rule.taint not in taints:
            taints.append(rule.taint)
    if len(result.encode("utf-8")) > MAX_PAYLOAD_BYTES * 2:
        raise _error("injected value is oversized")
    return InjectionApplication(result, tuple(applied), tuple(taints))


def _load_budget(value: object) -> RunBudget:
    if not isinstance(value, dict) or set(value) != {
        "max_turns",
        "max_tool_calls",
        "timeout_seconds",
        "max_output_tokens",
    }:
        raise _error("scenario budget has unexpected fields")
    turns = value.get("max_turns")
    calls = value.get("max_tool_calls")
    timeout = value.get("timeout_seconds")
    output_tokens = value.get("max_output_tokens")
    if type(turns) is not int or not 1 <= turns <= 64:
        raise _error("max_turns is invalid")
    if type(calls) is not int or not 0 <= calls <= 256:
        raise _error("max_tool_calls is invalid")
    if type(timeout) is not int or not 1 <= timeout <= 3_600:
        raise _error("timeout_seconds is invalid")
    if type(output_tokens) is not int or not 1 <= output_tokens <= 32_768:
        raise _error("max_output_tokens is invalid")
    return RunBudget(turns, calls, timeout, output_tokens)


def _load_injections(
    root: Path, value: object, *, private_ref: str
) -> tuple[InjectionRule, ...]:
    if not isinstance(value, list) or len(value) > 64:
        raise _error("scenario injections must be a bounded array")
    result: list[InjectionRule] = []
    seen: set[str] = set()
    allowed_taints = {item.value: item for item in Taint}
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "hook",
            "operation",
            "target",
            "taint",
            "payload_file",
        }:
            raise _error("scenario injection has unexpected fields")
        identifier = _checked_name(item.get("id"), "injection id")
        if identifier in seen:
            raise _error("scenario injection ids must be unique")
        seen.add(identifier)
        hook = item.get("hook")
        operation = item.get("operation")
        target = item.get("target")
        taint_value = item.get("taint")
        if hook not in _HOOKS or operation not in _OPERATIONS:
            raise _error("scenario injection has an invalid hook or operation")
        if target is not None:
            target = _checked_name(target, "injection target")
        if not isinstance(taint_value, str) or taint_value not in allowed_taints:
            raise _error("scenario injection has an invalid taint")
        payload_ref = _checked_ref(item.get("payload_file"), "injection payload")
        if payload_ref == private_ref:
            raise _error("private oracle cannot also be an injection payload")
        payload = _read_text(root, payload_ref, MAX_PAYLOAD_BYTES, "injection payload")
        if not payload.strip():
            raise _error("injection payload is empty")
        result.append(
            InjectionRule(
                identifier,
                hook,
                operation,
                target,
                allowed_taints[taint_value],
                payload,
            )
        )
    return tuple(result)


def _load_answer_oracle(value: object) -> AnswerOracle:
    if not isinstance(value, dict):
        raise _error("answer oracle has unexpected fields")
    mode = value.get("mode")
    forbidden = _checked_strings(
        value.get("forbidden_substrings"), "forbidden answer substrings"
    )
    if mode == "exact" and set(value) == {"mode", "value", "forbidden_substrings"}:
        exact = _checked_string(value.get("value"), "exact answer value")
        if exact != exact.strip():
            raise _error("exact answer value must already be trimmed")
        expected = (exact,)
    elif mode == "contains_all" and set(value) == {
        "mode",
        "values",
        "forbidden_substrings",
    }:
        expected = _checked_strings(value.get("values"), "required answer values")
    else:
        raise _error("answer oracle has unexpected fields or mode")
    if any(fragment in item for item in expected for fragment in forbidden):
        raise _error("answer oracle constraints overlap")
    return AnswerOracle(mode, expected, forbidden)


def _load_tool_oracle(value: object, scenario: ScenarioManifest) -> ToolOracle:
    if not isinstance(value, dict) or set(value) != {
        "required",
        "optional",
        "forbidden",
        "evidence",
    }:
        raise _error("tool oracle has unexpected fields")
    required = _checked_names(value.get("required"), "required tools")
    optional = _checked_names(value.get("optional"), "optional tools")
    forbidden = _checked_names(value.get("forbidden"), "forbidden tools")
    groups = (set(required), set(optional), set(forbidden))
    if groups[0] & groups[1] or groups[0] & groups[2] or groups[1] & groups[2]:
        raise _error("tool oracle groups overlap")
    if not (groups[0] | groups[1]) <= set(scenario.tools):
        raise _error("required and optional tools must be available to the scenario")
    evidence = _load_tool_evidence(value.get("evidence"), groups[0])
    return ToolOracle(required, optional, forbidden, evidence)


def _load_tool_evidence(
    value: object, task_tools: set[str]
) -> tuple[ToolEvidenceRule, ...]:
    if not isinstance(value, list) or len(value) > 64:
        raise _error("tool evidence must be a bounded array")
    rules: list[ToolEvidenceRule] = []
    for item in value:
        if not isinstance(item, dict) or set(item) not in (
            {"name", "arguments_subset"},
            {"name", "result_subset"},
            {"name", "arguments_subset", "result_subset"},
        ):
            raise _error("tool evidence rule has unexpected fields")
        name = _checked_name(item.get("name"), "tool evidence name")
        if name not in task_tools:
            raise _error("tool evidence must reference a required tool")
        arguments = (
            _checked_subset(item.get("arguments_subset"), "tool argument subset")
            if "arguments_subset" in item
            else None
        )
        result = (
            _checked_subset(item.get("result_subset"), "tool result subset")
            if "result_subset" in item
            else None
        )
        rules.append(ToolEvidenceRule(name, arguments, result))
    return tuple(rules)


def _checked_subset(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not value:
        raise _error(f"{label} must be a non-empty JSON object")

    def visit(item: object, depth: int = 0) -> None:
        if depth > 8:
            raise _error(f"{label} is too deeply nested")
        if item is None or isinstance(item, str | int | float | bool):
            return
        if isinstance(item, dict):
            if len(item) > 128 or any(not isinstance(key, str) for key in item):
                raise _error(f"{label} contains an invalid object")
            for child in item.values():
                visit(child, depth + 1)
            return
        if isinstance(item, list):
            if len(item) > 128:
                raise _error(f"{label} contains an oversized array")
            for child in item:
                visit(child, depth + 1)
            return
        raise _error(f"{label} must contain only JSON values")

    visit(value)
    return value


def _load_security_oracle(value: object, scenario: ScenarioManifest) -> SecurityOracle:
    if not isinstance(value, dict) or set(value) != {
        "canaries",
        "forbidden_canary_channels",
        "forbidden_output_substrings",
    }:
        raise _error("security oracle has unexpected fields")
    raw_canaries = value.get("canaries")
    if not isinstance(raw_canaries, list) or len(raw_canaries) > 64:
        raise _error("security canaries must be a bounded array")
    canaries: list[CanaryOracle] = []
    ids: set[str] = set()
    values: set[str] = set()
    for item in raw_canaries:
        if not isinstance(item, dict) or set(item) != {"id", "value"}:
            raise _error("security canary has unexpected fields")
        identifier = _checked_name(item.get("id"), "canary id")
        canary = _checked_string(item.get("value"), "canary value")
        if identifier in ids or canary in values:
            raise _error("security canaries must be unique")
        ids.add(identifier)
        values.add(canary)
        canaries.append(CanaryOracle(identifier, canary))
    channels = _checked_strings(
        value.get("forbidden_canary_channels"), "forbidden canary channels"
    )
    if not set(channels) <= _CANARY_CHANNELS:
        raise _error("security oracle has an invalid canary channel")
    forbidden_output = _checked_strings(
        value.get("forbidden_output_substrings"), "forbidden output substrings"
    )
    payload = "".join(rule.payload for rule in scenario.injections)
    for canary in canaries:
        if payload.count(canary.value) != 1:
            raise _error(
                "each private canary must occur in injection payloads exactly once"
            )
    return SecurityOracle(tuple(canaries), channels, forbidden_output)


def _checked_root(directory: Path) -> Path:
    root = Path(directory)
    try:
        if not root.is_dir() or root.is_symlink():
            raise _error("scenario directory is unavailable")
        return root.resolve(strict=True)
    except OSError as error:
        raise _error("scenario directory is unavailable") from error


def _resolve_file(root: Path, relative: str, label: str) -> Path:
    path = root.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise _error(f"{label} is unavailable") from error
    if not resolved.is_file() or path.is_symlink() or resolved != path:
        # resolved != path rejects a symlink in any parent component.
        raise _error(f"{label} is unavailable")
    return resolved


def _read_json(root: Path, relative: str, maximum: int, label: str) -> object:
    document = _read_text(root, relative, maximum, label)
    try:
        return strict_json(document)
    except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError) as error:
        raise _error(f"{label} is not strict JSON") from error


def _read_text(root: Path, relative: str, maximum: int, label: str) -> str:
    path = _resolve_file(root, relative, label)
    try:
        data = path.read_bytes()
    except OSError as error:
        raise _error(f"unable to read {label}") from error
    if not data or len(data) > maximum:
        raise _error(f"{label} is empty or oversized")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _error(f"{label} is not UTF-8") from error


def _checked_ref(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or len(value.encode("utf-8")) > 256
    ):
        raise _error(f"{label} path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(
            part in {"", ".", ".."} or not _PATH_PART.fullmatch(part)
            for part in path.parts
        )
    ):
        raise _error(f"{label} path is invalid")
    return path.as_posix()


def _checked_scenario_id(value: object) -> str:
    if not isinstance(value, str) or not _SCENARIO_ID.fullmatch(value):
        raise _error("scenario id is invalid")
    return value


def _checked_name(value: object, label: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise _error(f"{label} is invalid")
    return value


def _checked_names(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 128:
        raise _error(f"{label} must be a bounded array")
    result = tuple(_checked_name(item, label) for item in value)
    if len(set(result)) != len(result):
        raise _error(f"{label} contains duplicates")
    return result


def _checked_string(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > 4_096
    ):
        raise _error(f"{label} must be bounded non-empty text")
    return value


def _checked_strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 128:
        raise _error(f"{label} must be a bounded array")
    result = tuple(_checked_string(item, label) for item in value)
    if len(set(result)) != len(result):
        raise _error(f"{label} contains duplicates")
    return result


def _error(message: str) -> TargetError:
    return TargetError(ErrorCode.POLICY, f"scenario v2: {message}")


__all__ = [
    "AnswerOracle",
    "CanaryOracle",
    "InjectionApplication",
    "InjectionRule",
    "PrivateOracle",
    "RunBudget",
    "ScenarioManifest",
    "SecurityOracle",
    "ToolEvidenceRule",
    "ToolOracle",
    "apply_text_injections",
    "load_private_oracle",
    "load_scenario_manifest",
]
