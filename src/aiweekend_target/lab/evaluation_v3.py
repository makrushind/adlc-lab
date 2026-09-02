"""Independent declarative evaluation over private engine evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass

from aiweekend_target.core.contracts import BoundaryEvidence, detached_json
from aiweekend_target.core.engine import ToolCallRecord


_ASSERTION_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_GROUPS = {"runtime", "task", "security", "tool_selection"}
_OPERATORS = {"equals", "contains", "not_contains", "includes", "excludes", "subset_any"}


@dataclass(frozen=True)
class RunEvidenceV3:
    pipeline_ok: bool
    final_answer: str | None
    turns: int
    reported_tokens: int | None
    tool_calls: tuple[ToolCallRecord, ...]
    boundaries: tuple[BoundaryEvidence, ...]
    error_code: str | None = None


@dataclass(frozen=True)
class AssertionResult:
    id: str
    group: str
    ok: bool
    operator: str
    subject: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "group": self.group,
            "ok": self.ok,
            "operator": self.operator,
            "subject": self.subject,
        }


@dataclass(frozen=True)
class EvaluationReportV3:
    scenario_id: str
    pipeline_ok: bool
    task_success: bool
    security_ok: bool
    tool_selection_ok: bool
    runtime_ok: bool
    ok: bool
    assertions: tuple[AssertionResult, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": 3,
            "type": "evaluation_result",
            "scenario": self.scenario_id,
            "ok": self.ok,
            "pipeline_ok": self.pipeline_ok,
            "task_success": self.task_success,
            "security_ok": self.security_ok,
            "tool_selection_ok": self.tool_selection_ok,
            "runtime_ok": self.runtime_ok,
            "assertions": [item.as_dict() for item in self.assertions],
        }


def _subset(expected: object, actual: object) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _subset(value, actual[key]) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and all(
            any(_subset(item, candidate) for candidate in actual) for item in expected
        )
    return expected == actual


def _subject(evidence: RunEvidenceV3, subject: str) -> object:
    executed = tuple(item for item in evidence.tool_calls if item.executed)
    if subject == "pipeline_ok":
        return evidence.pipeline_ok
    if subject == "answer":
        return evidence.final_answer
    if subject == "executed_tools":
        return [item.name for item in executed]
    if subject == "finding_codes":
        return [finding.code for item in evidence.boundaries for finding in item.findings]
    if subject == "control_actions":
        return [
            f"{item.boundary.value}:{item.decision.action.value}" for item in evidence.boundaries
        ]
    if subject == "provenance":
        return [marker for item in evidence.boundaries for marker in item.provenance]
    if subject == "error_code":
        return evidence.error_code
    prefix = "tool_results."
    if subject.startswith(prefix) and len(subject) > len(prefix):
        name = subject[len(prefix) :]
        return [item.result for item in executed if item.name == name]
    prefix = "tool_arguments."
    if subject.startswith(prefix) and len(subject) > len(prefix):
        name = subject[len(prefix) :]
        return [item.arguments for item in executed if item.name == name]
    raise ValueError(f"oracle assertion subject is unsupported: {subject}")


def _evaluate(operator: str, actual: object, expected: object) -> bool:
    if operator == "equals":
        return actual == expected
    if operator in {"contains", "not_contains"}:
        result = isinstance(actual, str) and isinstance(expected, str) and expected in actual
        return not result if operator == "not_contains" else result
    if operator in {"includes", "excludes"}:
        result = isinstance(actual, list) and expected in actual
        return not result if operator == "excludes" else result
    if operator == "subset_any":
        return isinstance(actual, list) and any(_subset(expected, item) for item in actual)
    raise ValueError("oracle assertion operator is unsupported")


class DeclarativeEvaluator:
    id = "builtin.assertions"

    def validate_oracle(self, oracle: object) -> None:
        self.evaluate(RunEvidenceV3(False, None, 0, None, (), ()), oracle)

    def evaluate(self, evidence: object, oracle: object) -> EvaluationReportV3:
        if not isinstance(evidence, RunEvidenceV3):
            raise ValueError("evaluator requires private v3 evidence")
        if not isinstance(oracle, dict) or set(oracle) != {
            "schema",
            "scenario_id",
            "assertions",
        } or oracle.get("schema") != 1:
            raise ValueError("private oracle has unexpected fields or schema")
        scenario_id = oracle.get("scenario_id")
        assertions = oracle.get("assertions")
        if not isinstance(scenario_id, str) or not isinstance(assertions, list) or len(assertions) > 256:
            raise ValueError("private oracle is invalid")
        results: list[AssertionResult] = []
        seen: set[str] = set()
        for item in assertions:
            if not isinstance(item, dict) or set(item) != {
                "id",
                "group",
                "subject",
                "operator",
                "expected",
            }:
                raise ValueError("oracle assertion has unexpected fields")
            identifier = item.get("id")
            group = item.get("group")
            subject = item.get("subject")
            operator = item.get("operator")
            if (
                not isinstance(identifier, str)
                or not _ASSERTION_ID.fullmatch(identifier)
                or identifier in seen
                or group not in _GROUPS
                or not isinstance(subject, str)
                or len(subject) > 192
                or operator not in _OPERATORS
            ):
                raise ValueError("oracle assertion is invalid")
            seen.add(identifier)
            expected = detached_json(item.get("expected"), maximum=32 * 1024, label="oracle expected value")
            actual = _subject(evidence, subject)
            results.append(
                AssertionResult(identifier, group, _evaluate(operator, actual, expected), operator, subject)
            )
        group_ok = {
            group: all(item.ok for item in results if item.group == group)
            for group in _GROUPS
        }
        ok = evidence.pipeline_ok and all(item.ok for item in results)
        return EvaluationReportV3(
            scenario_id,
            evidence.pipeline_ok,
            group_ok["task"],
            group_ok["security"],
            group_ok["tool_selection"],
            group_ok["runtime"],
            ok,
            tuple(results),
        )


__all__ = [
    "AssertionResult",
    "DeclarativeEvaluator",
    "EvaluationReportV3",
    "RunEvidenceV3",
]
