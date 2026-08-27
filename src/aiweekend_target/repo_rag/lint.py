"""Deterministic, read-only Python linting for changed pull-request lines."""

from __future__ import annotations

import ast
import stat
from pathlib import Path, PurePosixPath
from typing import TypedDict, cast

from aiweekend_target.errors import ErrorCode, TargetError


MAX_TARGETS = 100
MAX_ADDED_LINES = 10_000
MAX_DIAGNOSTICS = 100
MAX_FILE_BYTES = 256 * 1024
RULE = "ADLC001"
SEVERITY = "high"
MESSAGE = "Avoid eval() on untrusted input"


class LintTarget(TypedDict):
    path: str
    added_lines: list[int]


class LintDiagnostic(TypedDict):
    path: str
    line: int
    column: int
    rule: str
    severity: str
    message: str


class LintResponse(TypedDict):
    diagnostics: list[LintDiagnostic]


def lint_pr(corpus_root: str | Path, targets: object) -> LintResponse:
    """Find direct ``eval`` calls that begin on explicitly changed lines."""
    root = _validated_root(corpus_root)
    checked_targets = _validate_targets(targets)
    diagnostics: list[LintDiagnostic] = []
    for target in checked_targets:
        source = _read_target(root, target["path"])
        try:
            tree = ast.parse(source, filename=target["path"])
        except SyntaxError as error:
            raise TargetError(ErrorCode.POLICY, "changed Python target is not syntactically valid") from error
        added_lines = set(target["added_lines"])
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "eval"
                and node.lineno in added_lines
            ):
                diagnostics.append(
                    {
                        "path": target["path"],
                        "line": node.lineno,
                        "column": node.col_offset + 1,
                        "rule": RULE,
                        "severity": SEVERITY,
                        "message": MESSAGE,
                    }
                )
    diagnostics.sort(key=lambda item: (item["path"], item["line"], item["column"]))
    return {"diagnostics": diagnostics[:MAX_DIAGNOSTICS]}


def validate_lint_response(value: object) -> LintResponse:
    """Validate a JSON-compatible lint response without reading or executing anything."""
    if not isinstance(value, dict) or set(value) != {"diagnostics"} or not isinstance(value["diagnostics"], list):
        raise TargetError(ErrorCode.MCP, "pull-request lint returned an invalid result")
    if len(value["diagnostics"]) > MAX_DIAGNOSTICS:
        raise TargetError(ErrorCode.MCP, "pull-request lint returned an invalid result")
    diagnostics: list[LintDiagnostic] = []
    for item in value["diagnostics"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "line", "column", "rule", "severity", "message"}
            or not _is_safe_python_path(item.get("path"))
            or not _is_positive_int(item.get("line"))
            or not _is_positive_int(item.get("column"))
            or item.get("rule") != RULE
            or item.get("severity") != SEVERITY
            or item.get("message") != MESSAGE
        ):
            raise TargetError(ErrorCode.MCP, "pull-request lint returned an invalid result")
        diagnostics.append(cast(LintDiagnostic, dict(item)))
    return {"diagnostics": diagnostics}


def _validated_root(corpus_root: str | Path) -> Path:
    root = Path(corpus_root)
    if not root.is_dir() or root.is_symlink():
        raise TargetError(ErrorCode.POLICY, "lint corpus root is unavailable")
    try:
        return root.resolve(strict=True)
    except OSError as error:
        raise TargetError(ErrorCode.MCP, "lint corpus root is unavailable") from error


def _validate_targets(value: object) -> list[LintTarget]:
    if not isinstance(value, list) or len(value) > MAX_TARGETS:
        raise TargetError(ErrorCode.POLICY, "lint targets are invalid")
    checked: list[LintTarget] = []
    line_count = 0
    for target in value:
        if (
            not isinstance(target, dict)
            or set(target) != {"path", "added_lines"}
            or not _is_safe_python_path(target.get("path"))
            or not isinstance(target.get("added_lines"), list)
            or not all(_is_positive_int(line) for line in target["added_lines"])
        ):
            raise TargetError(ErrorCode.POLICY, "lint targets are invalid")
        line_count += len(target["added_lines"])
        if line_count > MAX_ADDED_LINES:
            raise TargetError(ErrorCode.POLICY, "lint targets are invalid")
        checked.append(cast(LintTarget, dict(target)))
    return checked


def _read_target(root: Path, relative: str) -> str:
    target = root.joinpath(*PurePosixPath(relative).parts)
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise TargetError(ErrorCode.POLICY, "lint target must not be a symlink")
    try:
        resolved = target.resolve(strict=True)
        resolved.relative_to(root)
        mode = target.stat().st_mode
        if not stat.S_ISREG(mode):
            raise TargetError(ErrorCode.POLICY, "lint target is not a regular file")
        data = target.read_bytes()
    except TargetError:
        raise
    except (OSError, ValueError) as error:
        raise TargetError(ErrorCode.POLICY, "lint target is unavailable") from error
    if len(data) > MAX_FILE_BYTES:
        raise TargetError(ErrorCode.POLICY, "lint target is oversized")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TargetError(ErrorCode.POLICY, "lint target is not UTF-8") from error


def _is_safe_python_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and path.as_posix() == value and value.endswith(".py")


def _is_positive_int(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


__all__ = ["LintDiagnostic", "LintResponse", "LintTarget", "lint_pr", "validate_lint_response"]
