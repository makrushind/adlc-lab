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
from aiweekend_target.errors import ErrorCode, TargetError, match_gateway_error
from aiweekend_target.gateway import create_app
from aiweekend_target.lab.scenarios import LabPaths, load_scenario, reset_scenario, validate_scenarios
from aiweekend_target.lab.review_prepare import prepare_review
from aiweekend_target.lab.token import manage_hf_token
from aiweekend_target.repo_rag.server import health_http, serve


_WORKSPACE = Path("/target/workspace")
_CORPUS = Path("/target/corpus")
_RAG_INDEX = Path("/target/rag-index")
_DATABASE = _RAG_INDEX / "index.sqlite"
_MARKER = _RAG_INDEX / "scenario.json"
_SCENARIOS_ROOT = Path("/opt/adlc/scenarios")
_SCENARIO_REPOSITORY = _SCENARIOS_ROOT.parent
_SECRET = Path("/run/secrets/hf_token")
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


def _repo_rag() -> None:
    serve(_DATABASE, _MARKER, _SCENARIOS_ROOT)


def _hf_gateway() -> None:
    uvicorn.run(
        create_app(secret_path=_SECRET),
        host="0.0.0.0",
        port=8080,
        access_log=False,
        proxy_headers=False,
        server_header=False,
    )


def _health(service: str) -> None:
    if service == "repo-rag":
        asyncio.run(health_http())
        return
    if service == "hf-gateway":
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
    return {"ok": True, "scenarios": [scenario.id for scenario in scenarios]}


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
        if arguments == ["repo-rag"]:
            _repo_rag()
            return 0
        if arguments == ["hf-gateway"]:
            _hf_gateway()
            return 0
        if arguments in (["health", "repo-rag"], ["health", "hf-gateway"]):
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
