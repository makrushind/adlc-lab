"""Unified fixed-path command entrypoint for attack-lab containers."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import IO

import httpx
import uvicorn

from aiweekend_target.agent import run_agent
from aiweekend_target.autonomous_lab import inspect_selected_model, run_autonomous_scenario
from aiweekend_target.errors import ErrorCode, TargetError, match_gateway_error
from aiweekend_target.gateway import create_app
from aiweekend_target.lab.scenarios import LabPaths, load_scenario, reset_scenario, validate_scenarios
from aiweekend_target.lab.scenario_v2 import load_private_oracle, load_scenario_manifest
from aiweekend_target.lab.experiment import (
    execute_matrix,
    persist_private_evidence,
    persist_public_artifact,
    run_experiment,
)
from aiweekend_target.lab.scenario_v3 import (
    load_experiment_profile,
    load_private_oracle_v3,
    load_scenario_v3,
)
from aiweekend_target.lab.review_prepare import prepare_review
from aiweekend_target.lab.token import manage_hf_token
from aiweekend_target.pr_review import run_pr_review
from aiweekend_target.repo_rag.server import health_http, serve


_WORKSPACE = Path("/target/workspace")
_CORPUS = Path("/target/corpus")
_RAG_INDEX = Path("/target/rag-index")
_DATABASE = _RAG_INDEX / "index.sqlite"
_MARKER = _RAG_INDEX / "scenario.json"
_SCENARIOS_ROOT = Path("/opt/adlc/scenarios")
_PROFILES_ROOT = Path("/opt/adlc/profiles/v3")
_SCENARIO_REPOSITORY = _SCENARIOS_ROOT.parent
_GATEWAY_LIVE = "http://127.0.0.1:8080/health/live"
_GATEWAY_READY = "http://127.0.0.1:8080/health/ready"
_PREPARE_SOURCE = Path("/input/repo")
_PREPARE_DIFF = Path("/input/pr.diff")
_PREPARE_MARKER = Path("/opt/adlc/scenarios/baseline/scenario.json")


def _write(output: IO[str], value: dict[str, object]) -> None:
    output.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    output.flush()


def _reset() -> dict[str, object]:
    scenario_id = os.environ.get("LAB_SCENARIO", "baseline")
    scenario = load_scenario(_SCENARIO_REPOSITORY, scenario_id)
    paths = LabPaths(_WORKSPACE, _CORPUS, _RAG_INDEX)
    return reset_scenario(paths, scenario, _SCENARIO_REPOSITORY)


def _agent() -> int:
    return run_agent()


def _pr_review(output: IO[str]) -> int:
    return run_pr_review(output=output)


def _repo_rag() -> None:
    serve(_DATABASE, _MARKER, _SCENARIOS_ROOT)


def _model_gateway() -> None:
    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=8080,
        access_log=False,
        proxy_headers=False,
        server_header=False,
    )


_hf_gateway = _model_gateway


def _health(service: str) -> None:
    if service == "repo-rag":
        asyncio.run(health_http())
        return
    if service == "gateway-live":
        try:
            with httpx.Client(follow_redirects=False, timeout=3.0, trust_env=False) as client:
                response = client.get(_GATEWAY_LIVE)
        except httpx.HTTPError as error:
            raise TargetError(ErrorCode.PROVIDER, "gateway liveness contract failed") from error
        try:
            payload = response.json()
        except (UnicodeError, ValueError) as error:
            raise TargetError(ErrorCode.PROVIDER, "gateway liveness contract failed") from error
        if response.status_code == 200 and payload == {"status": "live"}:
            return
        raise TargetError(ErrorCode.PROVIDER, "gateway liveness contract failed")
    if service in {"hf-gateway", "model-gateway"}:
        try:
            with httpx.Client(follow_redirects=False, timeout=3.0, trust_env=False) as client:
                response = client.get(_GATEWAY_READY)
        except httpx.HTTPError as error:
            raise TargetError(ErrorCode.PROVIDER, "gateway readiness contract failed") from error
        try:
            payload = response.json()
        except (UnicodeError, ValueError) as error:
            raise TargetError(ErrorCode.PROVIDER, "gateway readiness contract failed") from error
        if response.status_code == 200 and payload == {"status": "ready"}:
            return
        failure_code = match_gateway_error(payload, response.status_code, readiness=True)
        if failure_code is not None:
            raise TargetError(failure_code, "gateway readiness contract failed")
        raise TargetError(ErrorCode.PROVIDER, "gateway readiness contract failed")
    raise TargetError(ErrorCode.CONFIG, "health service is not recognized")


def _validate() -> dict[str, object]:
    scenarios = validate_scenarios(_SCENARIO_REPOSITORY)
    autonomous: list[str] = []
    autonomous_root = _SCENARIOS_ROOT / "v2"
    if not autonomous_root.is_dir() or autonomous_root.is_symlink():
        raise TargetError(ErrorCode.CONFIG, "autonomous scenario root is unavailable")
    for directory in sorted(autonomous_root.iterdir(), key=lambda item: item.name):
        if not directory.is_dir() or directory.is_symlink():
            raise TargetError(
                ErrorCode.CONFIG, "autonomous scenario root has an invalid entry"
            )
        scenario = load_scenario_manifest(directory)
        if scenario.id != directory.name:
            raise TargetError(
                ErrorCode.CONFIG,
                "autonomous scenario directory does not match its descriptor",
            )
        load_private_oracle(directory, scenario)
        autonomous.append(scenario.id)
    if not autonomous:
        raise TargetError(ErrorCode.CONFIG, "autonomous scenarios are unavailable")
    experiments: list[str] = []
    experiment_root = _SCENARIOS_ROOT / "v3"
    if not experiment_root.is_dir() or experiment_root.is_symlink():
        raise TargetError(ErrorCode.CONFIG, "v3 experiment root is unavailable")
    for directory in sorted(experiment_root.iterdir(), key=lambda item: item.name):
        scenario = load_scenario_v3(directory)
        if scenario.id != directory.name:
            raise TargetError(
                ErrorCode.CONFIG,
                "v3 experiment directory does not match its descriptor",
            )
        load_private_oracle_v3(directory, scenario)
        experiments.append(scenario.id)
    profiles: list[str] = []
    if not _PROFILES_ROOT.is_dir() or _PROFILES_ROOT.is_symlink():
        raise TargetError(ErrorCode.CONFIG, "v3 profile root is unavailable")
    for path in sorted(_PROFILES_ROOT.glob("*.json"), key=lambda item: item.name):
        profile = load_experiment_profile(path)
        if path.stem != profile.id:
            raise TargetError(ErrorCode.CONFIG, "v3 profile filename does not match its id")
        profiles.append(profile.id)
    return {
        "ok": True,
        "scenarios": [scenario.id for scenario in scenarios],
        "autonomous_scenarios": autonomous,
        "experiments": experiments,
        "experiment_profiles": profiles,
    }


def _experiment_path(identifier: str, *, profile: bool = False) -> Path:
    if not identifier or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in identifier
    ):
        raise TargetError(ErrorCode.CONFIG, "experiment identifier is invalid")
    return (
        _PROFILES_ROOT / f"{identifier}.json"
        if profile
        else _SCENARIOS_ROOT / "v3" / identifier
    )


def _artifact_root() -> Path | None:
    value = os.environ.get("ADLC_ARTIFACT_ROOT")
    if value is None:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise TargetError(ErrorCode.CONFIG, "ADLC_ARTIFACT_ROOT must be absolute")
    return path


def _private_evidence_root() -> Path | None:
    value = os.environ.get("ADLC_PRIVATE_EVIDENCE_ROOT")
    if value is None:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise TargetError(
            ErrorCode.CONFIG, "ADLC_PRIVATE_EVIDENCE_ROOT must be absolute"
        )
    return path


def _experiment_matrix(
    scenario_ids: list[str], profile_ids: list[str], repeats: int, output: IO[str]
) -> int:
    outcomes = asyncio.run(
        execute_matrix(
            [_experiment_path(item) for item in scenario_ids],
            [_experiment_path(item, profile=True) for item in profile_ids],
            repeats=repeats,
        )
    )
    artifact_root = _artifact_root()
    private_root = _private_evidence_root()
    passed = 0
    failed = 0
    setup_failed = 0
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            setup_failed += 1
            _write(output, {"schema": 3, "ok": False, "error": "matrix_run_setup_failed"})
            continue
        document = outcome.public_report()
        if artifact_root is not None:
            document["artifact_directory"] = str(
                persist_public_artifact(outcome, artifact_root)
            )
        if private_root is not None:
            document["private_evidence_file"] = str(
                persist_private_evidence(outcome, private_root)
            )
        _write(output, document)
        if outcome.evaluation.ok:
            passed += 1
        else:
            failed += 1
    _write(
        output,
        {
            "schema": 3,
            "type": "matrix_summary",
            "ok": failed == 0 and setup_failed == 0,
            "runs": len(outcomes),
            "passed": passed,
            "failed": failed,
            "setup_failed": setup_failed,
        },
    )
    return 0 if failed == 0 and setup_failed == 0 else 1


def _prepare_review() -> dict[str, object]:
    return prepare_review(
        LabPaths(_WORKSPACE, _CORPUS, _RAG_INDEX),
        _PREPARE_SOURCE,
        _PREPARE_DIFF,
        _PREPARE_MARKER,
    )


def main(argv: list[str] | None = None, *, output: IO[str] = sys.stdout) -> int:
    """Dispatch only the fixed lab command forms."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments == ["reset"]:
            _write(output, _reset())
            return 0
        if arguments == ["prepare-review"]:
            _write(output, _prepare_review())
            return 0
        if arguments == ["agent"]:
            return _agent()
        if arguments == ["pr-review"]:
            return _pr_review(output)
        if len(arguments) == 2 and arguments[0] == "run":
            return run_autonomous_scenario(arguments[1], output=output)
        if len(arguments) == 4 and arguments[:2] == ["experiment", "run"]:
            return run_experiment(
                _experiment_path(arguments[2]),
                _experiment_path(arguments[3], profile=True),
                output=output,
                artifact_root=_artifact_root(),
                private_evidence_root=_private_evidence_root(),
            )
        if len(arguments) == 5 and arguments[:2] == ["experiment", "matrix"]:
            try:
                repeats = int(arguments[4])
            except ValueError as error:
                raise TargetError(ErrorCode.CONFIG, "matrix repeats is invalid") from error
            return _experiment_matrix(
                arguments[2].split(","),
                arguments[3].split(","),
                repeats,
                output,
            )
        if arguments == ["models"]:
            return inspect_selected_model(output=output)
        if arguments == ["models", "doctor"]:
            return inspect_selected_model(output=output, tool_probe=True)
        if arguments == ["repo-rag"]:
            _repo_rag()
            return 0
        if arguments == ["hf-gateway"]:
            _hf_gateway()
            return 0
        if arguments == ["model-gateway"]:
            _model_gateway()
            return 0
        if arguments in (
            ["health", "repo-rag"],
            ["health", "gateway-live"],
            ["health", "hf-gateway"],
            ["health", "model-gateway"],
        ):
            _health(arguments[1])
            return 0
        if arguments == ["validate-scenarios"]:
            _write(output, _validate())
            return 0
        if arguments == ["setup-token"]:
            _write(output, manage_hf_token("create"))
            return 0
        if arguments == ["setup-token", "delete"]:
            _write(output, manage_hf_token("delete"))
            return 0
        return 2
    except TargetError as error:
        _write(output, error.as_result())
        return 1
    except Exception:
        error = TargetError(ErrorCode.CONFIG, "command failed")
        _write(output, error.as_result())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
