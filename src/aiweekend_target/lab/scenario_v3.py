"""Strict inert manifests compiled through the trusted component catalog."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from aiweekend_target.agent_protocol import strict_json
from aiweekend_target.core.catalog import (
    ComponentCatalog,
    ComponentKind,
    ComponentRef,
)
from aiweekend_target.core.contracts import (
    Analyzer,
    AttackTransform,
    ControlPolicy,
    Evaluator,
    ModelProvider,
)
from aiweekend_target.core.engine import AgentLimits
from aiweekend_target.tools import CompositeToolProvider, ToolProvider


MAX_MANIFEST_BYTES = 64 * 1024
MAX_TASK_BYTES = 64 * 1024
MAX_ORACLE_BYTES = 64 * 1024
_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_TOOL = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ScenarioV3Error(ValueError):
    pass


@dataclass(frozen=True)
class ScenarioManifestV3:
    id: str
    task: str
    required_capabilities: tuple[str, ...]
    limits: AgentLimits
    tool_providers: tuple[ComponentRef, ...]
    tool_allowlist: tuple[str, ...]
    attacks: tuple[ComponentRef, ...]
    evaluator: ComponentRef
    oracle_ref: str
    digest: str


@dataclass(frozen=True)
class ExperimentProfile:
    id: str
    model: ComponentRef
    analyzers: tuple[ComponentRef, ...]
    policy: ComponentRef
    digest: str


@dataclass(frozen=True)
class RunPlan:
    scenario: ScenarioManifestV3
    profile: ExperimentProfile
    model: ModelProvider
    tools: ToolProvider
    attacks: tuple[AttackTransform, ...]
    analyzers: tuple[Analyzer, ...]
    policy: ControlPolicy
    evaluator: Evaluator
    oracle: object
    component_manifest: tuple[dict[str, object], ...]


def _root(path: Path) -> Path:
    if not isinstance(path, Path):
        raise ScenarioV3Error("scenario root must be a path")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir() or resolved.is_symlink():
        raise ScenarioV3Error("scenario root is invalid")
    return resolved


def _ref(root: Path, value: object, label: str) -> tuple[Path, str]:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ScenarioV3Error(f"{label} is invalid")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or any(not part for part in pure.parts):
        raise ScenarioV3Error(f"{label} is invalid")
    target = root.joinpath(*pure.parts).resolve(strict=True)
    if target.is_symlink() or not target.is_file() or not target.is_relative_to(root):
        raise ScenarioV3Error(f"{label} is invalid")
    return target, pure.as_posix()


def _read_bytes(path: Path, maximum: int, label: str) -> bytes:
    size = path.stat().st_size
    if size < 1 or size > maximum:
        raise ScenarioV3Error(f"{label} size is invalid")
    value = path.read_bytes()
    if len(value) != size:
        raise ScenarioV3Error(f"{label} changed while loading")
    return value


def _json_file(root: Path, reference: object, maximum: int, label: str) -> tuple[object, str]:
    path, normalized = _ref(root, reference, label)
    try:
        value = strict_json(_read_bytes(path, maximum, label).decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise ScenarioV3Error(f"{label} must be strict JSON") from error
    return value, normalized


def _text_file(root: Path, reference: object, maximum: int, label: str) -> tuple[str, str]:
    path, normalized = _ref(root, reference, label)
    try:
        value = _read_bytes(path, maximum, label).decode("utf-8")
    except UnicodeError as error:
        raise ScenarioV3Error(f"{label} must be UTF-8") from error
    if not value.strip():
        raise ScenarioV3Error(f"{label} is empty")
    return value, normalized


def _digest(*values: object) -> str:
    document = json.dumps(values, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(document.encode("utf-8")).hexdigest()


def _component(value: object, kind: ComponentKind, label: str) -> ComponentRef:
    if not isinstance(value, dict) or set(value) != {"id", "version", "config"}:
        raise ScenarioV3Error(f"{label} has unexpected fields")
    try:
        return ComponentRef(kind, value["id"], value["version"], value["config"])
    except (KeyError, ValueError) as error:
        raise ScenarioV3Error(f"{label} is invalid") from error


def _components(
    value: object, kind: ComponentKind, label: str, *, maximum: int = 32
) -> tuple[ComponentRef, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ScenarioV3Error(f"{label} must be a bounded array")
    result = tuple(_component(item, kind, label) for item in value)
    identities = [
        (
            *item.key,
            json.dumps(dict(item.config), sort_keys=True, separators=(",", ":")),
        )
        for item in result
    ]
    if len(identities) != len(set(identities)):
        raise ScenarioV3Error(f"{label} contains duplicates")
    return result


def _strings(value: object, label: str, pattern: re.Pattern[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 128:
        raise ScenarioV3Error(f"{label} must be a bounded array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not pattern.fullmatch(item):
            raise ScenarioV3Error(f"{label} contains an invalid value")
        if item in result:
            raise ScenarioV3Error(f"{label} contains duplicates")
        result.append(item)
    return tuple(result)


def _limits(value: object) -> AgentLimits:
    expected = {
        "max_turns",
        "max_tool_calls",
        "max_identical_tool_calls",
        "timeout_seconds",
        "max_output_tokens",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ScenarioV3Error("scenario budget has unexpected fields")
    try:
        limits = AgentLimits(
            max_turns=value["max_turns"],
            max_tool_calls=value["max_tool_calls"],
            max_identical_tool_calls=value["max_identical_tool_calls"],
            max_wall_seconds=value["timeout_seconds"],
            max_output_tokens=value["max_output_tokens"],
        )
    except (KeyError, ValueError) as error:
        raise ScenarioV3Error("scenario budget is invalid") from error
    if limits.max_turns > 64 or limits.max_tool_calls > 256 or limits.max_wall_seconds > 3_600:
        raise ScenarioV3Error("scenario budget exceeds the host maximum")
    return limits


def load_scenario_v3(directory: Path) -> ScenarioManifestV3:
    root = _root(directory)
    value, _ = _json_file(root, "scenario.json", MAX_MANIFEST_BYTES, "scenario manifest")
    fields = {
        "schema",
        "id",
        "task_file",
        "required_capabilities",
        "budget",
        "tool_providers",
        "tool_allowlist",
        "attacks",
        "evaluator",
        "oracle_file",
    }
    if not isinstance(value, dict) or set(value) != fields or value.get("schema") != 3:
        raise ScenarioV3Error("scenario manifest has unexpected fields or schema")
    scenario_id = value.get("id")
    if not isinstance(scenario_id, str) or not _ID.fullmatch(scenario_id):
        raise ScenarioV3Error("scenario id is invalid")
    task, task_ref = _text_file(root, value.get("task_file"), MAX_TASK_BYTES, "scenario task")
    _, oracle_ref = _ref(root, value.get("oracle_file"), "private oracle")
    if task_ref == oracle_ref:
        raise ScenarioV3Error("private oracle cannot also be the public task")
    tools = _components(value.get("tool_providers"), ComponentKind.TOOL_PROVIDER, "tool providers")
    allowlist = _strings(value.get("tool_allowlist"), "tool allowlist", _TOOL)
    if bool(tools) != bool(allowlist):
        raise ScenarioV3Error("tool providers and allowlist must both be empty or non-empty")
    attacks = _components(value.get("attacks"), ComponentKind.ATTACK, "attacks")
    evaluator = _component(value.get("evaluator"), ComponentKind.EVALUATOR, "evaluator")
    capabilities = _strings(
        value.get("required_capabilities"), "required capabilities", _CAPABILITY
    )
    limits = _limits(value.get("budget"))
    public_for_digest = {key: item for key, item in value.items() if key != "oracle_file"}
    return ScenarioManifestV3(
        scenario_id,
        task,
        capabilities,
        limits,
        tools,
        allowlist,
        attacks,
        evaluator,
        oracle_ref,
        _digest(public_for_digest, task),
    )


def load_experiment_profile(path: Path) -> ExperimentProfile:
    if not isinstance(path, Path):
        raise ScenarioV3Error("profile path is invalid")
    resolved = path.resolve(strict=True)
    if resolved.is_symlink() or not resolved.is_file():
        raise ScenarioV3Error("profile path is invalid")
    try:
        raw = _read_bytes(resolved, MAX_MANIFEST_BYTES, "experiment profile")
        value = strict_json(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise ScenarioV3Error("experiment profile must be strict JSON") from error
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "id",
        "model",
        "analyzers",
        "policy",
    } or value.get("schema") != 1:
        raise ScenarioV3Error("experiment profile has unexpected fields or schema")
    profile_id = value.get("id")
    if not isinstance(profile_id, str) or not _ID.fullmatch(profile_id):
        raise ScenarioV3Error("profile id is invalid")
    model = _component(value.get("model"), ComponentKind.MODEL, "model")
    analyzers = _components(value.get("analyzers"), ComponentKind.ANALYZER, "analyzers")
    policy = _component(value.get("policy"), ComponentKind.POLICY, "policy")
    return ExperimentProfile(profile_id, model, analyzers, policy, _digest(value))


def load_private_oracle_v3(directory: Path, scenario: ScenarioManifestV3) -> object:
    root = _root(directory)
    value, normalized = _json_file(root, scenario.oracle_ref, MAX_ORACLE_BYTES, "private oracle")
    if normalized != scenario.oracle_ref:
        raise ScenarioV3Error("private oracle reference changed")
    if not isinstance(value, dict) or value.get("scenario_id") != scenario.id:
        raise ScenarioV3Error("private oracle does not match the scenario")
    return value


def _require_contract(value: object, methods: Sequence[str], label: str) -> object:
    if any(not callable(getattr(value, method, None)) for method in methods):
        raise ScenarioV3Error(f"trusted {label} does not implement its contract")
    return value


async def compile_run_plan(
    scenario: ScenarioManifestV3,
    profile: ExperimentProfile,
    oracle: object,
    catalog: ComponentCatalog,
) -> RunPlan:
    references = (
        profile.model,
        *scenario.tool_providers,
        *scenario.attacks,
        *profile.analyzers,
        profile.policy,
        scenario.evaluator,
    )
    try:
        catalog.preflight(references)
    except ValueError as error:
        raise ScenarioV3Error("component preflight failed") from error
    model = _require_contract(catalog.resolve(profile.model), ("describe", "complete"), "model")
    providers = tuple(
        _require_contract(catalog.resolve(item), ("list_tools", "call_tool"), "tool provider")
        for item in scenario.tool_providers
    )
    tools = CompositeToolProvider(providers, allowlist=scenario.tool_allowlist)
    attacks = tuple(
        _require_contract(catalog.resolve(item), ("transform",), "attack")
        for item in scenario.attacks
    )
    analyzers = tuple(
        _require_contract(catalog.resolve(item), ("analyze",), "analyzer")
        for item in profile.analyzers
    )
    policy = _require_contract(catalog.resolve(profile.policy), ("decide",), "policy")
    evaluator = _require_contract(
        catalog.resolve(scenario.evaluator), ("validate_oracle", "evaluate"), "evaluator"
    )
    evaluator.validate_oracle(oracle)
    specs = await tools.list_tools()
    descriptor = await model.describe()
    required = set(scenario.required_capabilities) | {"chat_completions"}
    if specs:
        required.add("tool_calls")
    missing = required - descriptor.capabilities
    if missing:
        raise ScenarioV3Error(
            f"selected model lacks capabilities: {', '.join(sorted(missing))}"
        )
    manifest = tuple(
        {
            "kind": item.kind.value,
            "id": item.id,
            "version": item.version,
            "config_digest": _digest(dict(item.config)),
        }
        for item in references
    )
    return RunPlan(
        scenario,
        profile,
        model,  # type: ignore[arg-type]
        tools,
        attacks,  # type: ignore[arg-type]
        analyzers,  # type: ignore[arg-type]
        policy,  # type: ignore[arg-type]
        evaluator,  # type: ignore[arg-type]
        oracle,
        manifest,
    )


__all__ = [
    "ExperimentProfile",
    "RunPlan",
    "ScenarioManifestV3",
    "ScenarioV3Error",
    "compile_run_plan",
    "load_experiment_profile",
    "load_private_oracle_v3",
    "load_scenario_v3",
]
