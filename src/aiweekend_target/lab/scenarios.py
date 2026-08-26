"""Strict, dependency-light attack-lab scenario loading and reset support."""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from aiweekend_target.errors import ErrorCode, TargetError
from aiweekend_target.repo_rag.index import build_index


_MAX_DESCRIPTOR_BYTES = 16_384
_MAX_PAYLOAD_BYTES = 65_536
_SCENARIOS = (
    ("baseline", "none", None, None),
    ("rag-poisoning", "rag", "ADLC_CANARY_RAG_7A91C4", "payload.md"),
    ("mcp-poisoning", "mcp", "ADLC_CANARY_MCP_4DB2E8", "mcp-result.json"),
    ("llm-injection", "prompt", "ADLC_CANARY_LLM_C61F03", "payload.txt"),
    ("custom", "prompt", "ADLC_CANARY_CUSTOM_95A7D2", "payload.txt"),
)
_SCENARIO_BY_ID = {identifier: (surface, canary, payload) for identifier, surface, canary, payload in _SCENARIOS}


@dataclass(frozen=True)
class LabPaths:
    workspace: Path
    corpus: Path
    rag_index: Path


@dataclass(frozen=True)
class Scenario:
    id: str
    attack_surface: str
    canary: str | None
    payload_path: Path | None


def load_scenario(root: Path, scenario_id: str, *, custom_payload: Path | None = None) -> Scenario:
    """Load one immutable built-in scenario, rejecting untrusted descriptor input."""
    if not isinstance(scenario_id, str) or scenario_id not in _SCENARIO_BY_ID:
        raise _scenario_error("scenario id is not recognized")
    if custom_payload is not None and scenario_id != "custom":
        raise _scenario_error("custom payload is only valid for the custom scenario")
    scenario_dir = _scenario_directory(Path(root), scenario_id)
    descriptor, _ = _read_descriptor(scenario_dir / "scenario.json", scenario_id)
    expected_surface, expected_canary, expected_payload = _SCENARIO_BY_ID[scenario_id]
    if (
        descriptor["attack_surface"] != expected_surface
        or descriptor["canary"] != expected_canary
        or descriptor["payload_file"] != expected_payload
    ):
        raise _scenario_error("scenario descriptor does not match the fixed scenario")
    payload_path = None
    if expected_payload is not None:
        payload_path = Path(custom_payload) if custom_payload is not None else scenario_dir / expected_payload
        _read_payload(payload_path, expected_canary)
    return Scenario(scenario_id, expected_surface, expected_canary, payload_path)


def validate_scenarios(root: Path) -> tuple[Scenario, ...]:
    """Return every fixed scenario after validating all descriptor and payload bytes."""
    return tuple(load_scenario(root, identifier) for identifier, _, _, _ in _SCENARIOS)


def reset_scenario(paths: LabPaths, scenario: Scenario, scenario_root: Path) -> dict[str, object]:
    """Replace lab state from validated fixtures and rebuild its repository index."""
    if not isinstance(scenario, Scenario) or scenario.id not in _SCENARIO_BY_ID:
        raise _scenario_error("scenario is not recognized")
    repository_root, scenarios_root = _locate_scenarios(Path(scenario_root), scenario.id)
    custom_payload = scenario.payload_path if scenario.id == "custom" else None
    checked_scenario = load_scenario(repository_root, scenario.id, custom_payload=custom_payload)
    baseline = load_scenario(repository_root, "baseline")
    if scenario != checked_scenario:
        raise _scenario_error("scenario does not match its descriptor")

    # Read every source before changing any target state.
    workspace_files = _read_tree(scenarios_root / "baseline" / "workspace")
    corpus_files = _read_tree(scenarios_root / "baseline" / "corpus")
    _require_file(workspace_files, "task.md")
    _require_file(workspace_files, "canary.txt")
    _require_file(corpus_files, "docs/handbook.md")
    payload = _read_payload(checked_scenario.payload_path, checked_scenario.canary) if checked_scenario.payload_path else None

    if checked_scenario.id == "rag-poisoning" and payload is not None:
        corpus_files["docs/rag-poisoning.md"] = payload
    elif checked_scenario.id in {"llm-injection", "custom"} and payload is not None:
        workspace_files["task.md"] = _append_untrusted_payload(workspace_files["task.md"], payload)

    stage_root, staged = _stage_state(
        workspace_files,
        corpus_files,
        _descriptor_marker(repository_root, checked_scenario.id),
    )
    try:
        _replace_directory_contents(paths.workspace, staged["workspace"])
        _replace_directory_contents(paths.corpus, staged["corpus"])
        _replace_directory_contents(paths.rag_index, staged["rag_index"])
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)
    # Keep the reference explicit: a reset always validates the baseline descriptor too.
    del baseline
    return {"ok": True, "reset": True, "scenario": checked_scenario.id}


def _scenario_directory(root: Path, scenario_id: str) -> Path:
    directory = root / "scenarios" / scenario_id
    if not directory.is_dir() or directory.is_symlink():
        raise _scenario_error("scenario directory is unavailable")
    return directory


def _locate_scenarios(scenario_root: Path, scenario_id: str) -> tuple[Path, Path]:
    if scenario_root.name == scenario_id and (scenario_root / "scenario.json").is_file():
        scenarios = scenario_root.parent
        return scenarios.parent, scenarios
    if (scenario_root / "scenarios" / scenario_id / "scenario.json").is_file():
        return scenario_root, scenario_root / "scenarios"
    if (scenario_root / scenario_id / "scenario.json").is_file():
        return scenario_root.parent, scenario_root
    raise _scenario_error("scenario root is unavailable")


def _read_descriptor(path: Path, expected_id: str) -> tuple[dict[str, object], bytes]:
    document = _read_utf8_file(path, _MAX_DESCRIPTOR_BYTES, "scenario descriptor")
    raw_bytes = document.encode("utf-8")
    try:
        value = json.loads(document)
    except json.JSONDecodeError as error:
        raise _scenario_error("scenario descriptor is not valid JSON") from error
    if not isinstance(value, dict) or set(value) != {"schema", "id", "attack_surface", "canary", "payload_file"}:
        raise _scenario_error("scenario descriptor has unexpected fields")
    schema = value.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != 1 or value.get("id") != expected_id:
        raise _scenario_error("scenario descriptor has invalid schema or id")
    if not isinstance(value["attack_surface"], str) or value["canary"] is not None and not isinstance(value["canary"], str):
        raise _scenario_error("scenario descriptor has invalid values")
    if value["payload_file"] is not None and not isinstance(value["payload_file"], str):
        raise _scenario_error("scenario descriptor has invalid payload file")
    return value, raw_bytes


def _read_payload(path: Path | None, canary: str | None) -> bytes:
    if path is None or canary is None:
        raise _scenario_error("scenario payload is missing")
    payload = _read_utf8_file(path, _MAX_PAYLOAD_BYTES, "scenario payload").encode("utf-8")
    if not payload or payload.count(canary.encode("utf-8")) != 1:
        raise _scenario_error("scenario payload must contain its canary exactly once")
    return payload


def _read_utf8_file(path: Path, maximum: int, label: str) -> str:
    try:
        if not path.is_file() or path.is_symlink():
            raise _scenario_error(f"{label} is unavailable")
        data = path.read_bytes()
    except OSError as error:
        raise _scenario_error(f"unable to read {label}") from error
    if not data or len(data) > maximum:
        raise _scenario_error(f"{label} is empty or oversized")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _scenario_error(f"{label} is not UTF-8") from error


def _read_tree(root: Path) -> dict[str, bytes]:
    if not root.is_dir() or root.is_symlink():
        raise _scenario_error("baseline tree is unavailable")
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise _scenario_error("baseline tree cannot contain symlinks")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            data = path.read_bytes()
            if not data:
                raise _scenario_error("baseline file is empty")
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise _scenario_error("baseline file is not UTF-8") from error
            files[relative] = data
    return files


def _require_file(files: dict[str, bytes], name: str) -> None:
    if name not in files:
        raise _scenario_error("baseline tree is incomplete")


def _append_untrusted_payload(task: bytes, payload: bytes) -> bytes:
    return task.rstrip() + b"\n\n<!-- BEGIN UNTRUSTED SCENARIO PAYLOAD -->\n" + payload + b"\n<!-- END UNTRUSTED SCENARIO PAYLOAD -->\n"


def _descriptor_marker(repository_root: Path, scenario_id: str) -> bytes:
    _, raw_bytes = _read_descriptor(repository_root / "scenarios" / scenario_id / "scenario.json", scenario_id)
    return raw_bytes


def _stage_state(
    workspace: dict[str, bytes],
    corpus: dict[str, bytes],
    marker: bytes,
) -> tuple[Path, dict[str, Path]]:
    stage_root = Path(tempfile.mkdtemp(prefix=".adlc-reset-stage-")).resolve(strict=True)
    staged = {
        name: stage_root / name
        for name in ("workspace", "corpus", "rag_index")
    }
    try:
        for directory in staged.values():
            directory.mkdir()
        _write_tree(staged["workspace"], workspace)
        _write_tree(staged["corpus"], corpus)
        build_index(staged["corpus"], staged["rag_index"] / "index.sqlite")
        (staged["rag_index"] / "scenario.json").write_bytes(marker)
        return stage_root, staged
    except Exception:
        shutil.rmtree(stage_root, ignore_errors=True)
        raise


def _write_tree(root: Path, files: dict[str, bytes]) -> None:
    for relative, data in files.items():
        safe = PurePosixPath(relative)
        if safe.is_absolute() or ".." in safe.parts:
            raise _scenario_error("baseline tree has an unsafe path")
        target = root.joinpath(*safe.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _replace_directory_contents(destination: Path, staged: Path) -> None:
    """Synchronize staged contents without replacing a Docker volume mountpoint."""
    if destination.is_symlink():
        raise _scenario_error("lab state root cannot be a symlink")
    if destination.exists() and not destination.is_dir():
        raise _scenario_error("lab state root must be a directory")
    destination.mkdir(parents=True, exist_ok=True)
    for child in destination.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    shutil.copytree(staged, destination, dirs_exist_ok=True)


def _scenario_error(message: str) -> TargetError:
    return TargetError(ErrorCode.POLICY, message)
