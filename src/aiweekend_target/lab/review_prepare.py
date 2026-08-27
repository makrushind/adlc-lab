"""Validate and stage a filtered pull-request checkout for repository review."""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from aiweekend_target.errors import ErrorCode, TargetError
from aiweekend_target.lab.scenarios import LabPaths, _replace_directory_contents
from aiweekend_target.repo_rag.index import build_index


_MAX_DIFF_BYTES = 512 * 1024
_MAX_FILES = 1000
_MAX_FILE_BYTES = 256 * 1024
_MAX_TOTAL_BYTES = 10 * 1024 * 1024
_ALLOWED_SUFFIXES = frozenset({".py", ".pyi", ".md", ".toml", ".yaml", ".yml", ".json", ".txt"})
_SKIPPED_DIRECTORIES = frozenset({
    ".git", ".venv", "venv", "env", "node_modules", "vendor", "build", "dist", "cache", ".cache",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
})
_HUNK = re.compile(r"^@@ -(?P<old_start>0|[1-9][0-9]*)(?:,(?P<old_count>[0-9]+))? \+(?P<new_start>0|[1-9][0-9]*)(?:,(?P<new_count>[0-9]+))? @@(?: .*)?$")


@dataclass(frozen=True)
class ReviewChange:
    """A single changed PR-head path and its literal added line numbers."""

    path: str
    added_lines: tuple[int, ...]
    deleted: bool


def parse_unified_diff(document: str) -> tuple[ReviewChange, ...]:
    """Parse one bounded, text-only Git unified diff into immutable changes."""
    if not isinstance(document, str) or not document or "\x00" in document:
        raise _policy("diff must be non-empty UTF-8 text")
    try:
        encoded = document.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _policy("diff must be UTF-8 text") from error
    if len(encoded) > _MAX_DIFF_BYTES or "GIT binary patch" in document or re.search(r"^Binary files .+ differ$", document, re.MULTILINE):
        raise _policy("diff is binary or oversized")

    lines = document.splitlines()
    changes: list[ReviewChange] = []
    seen_record_paths: set[str] = set()
    position = 0
    while position < len(lines):
        if not lines[position].startswith("diff --git "):
            raise _policy("diff has a malformed file header")
        header = lines[position]
        position += 1
        record: list[str] = []
        while position < len(lines) and not lines[position].startswith("diff --git "):
            record.append(lines[position])
            position += 1
        old_from_git, new_from_git = _git_paths(header, record)
        if {old_from_git, new_from_git} & seen_record_paths:
            raise _policy("diff has duplicate contradictory file records")
        seen_record_paths.update((old_from_git, new_from_git))
        changes.append(_parse_record(old_from_git, new_from_git, record))

    if not changes:
        raise _policy("diff has no changes")
    seen: set[str] = set()
    for change in changes:
        if change.path in seen:
            raise _policy("diff has duplicate file records")
        seen.add(change.path)
    return tuple(changes)


def prepare_review(paths: LabPaths, source_root: Path, diff_path: Path, baseline_marker: Path) -> dict[str, object]:
    """Stage a validated review corpus, diff, index and marker before replacing volumes."""
    source = Path(source_root)
    diff = _read_diff(Path(diff_path))
    changes = parse_unified_diff(diff)
    files = _read_filtered_checkout(source)
    _validate_changed_targets(changes, source)
    marker = _read_regular(Path(baseline_marker), "baseline marker")

    stage_root: Path | None = None
    try:
        stage_root = Path(tempfile.mkdtemp(prefix=".adlc-review-stage-")).resolve(strict=True)
        staged_workspace = stage_root / "workspace"
        staged_corpus = stage_root / "corpus"
        staged_index = stage_root / "rag_index"
        staged_workspace.mkdir()
        staged_corpus.mkdir()
        staged_index.mkdir()
        (staged_workspace / "pr.diff").write_text(diff.rstrip("\r\n") + "\n", encoding="utf-8")
        _write_tree(staged_corpus, files)
        build_index(staged_corpus, staged_index / "index.sqlite")
        (staged_index / "scenario.json").write_bytes(marker)

        _replace_directory_contents(paths.workspace, staged_workspace)
        _replace_directory_contents(paths.corpus, staged_corpus)
        _replace_directory_contents(paths.rag_index, staged_index)
    except TargetError:
        raise
    except (OSError, ValueError) as error:
        raise _policy("unable to prepare review state") from error
    finally:
        if stage_root is not None:
            shutil.rmtree(stage_root, ignore_errors=True)
    return {
        "ok": True,
        "prepared": True,
        "copied_files": len(files),
        "copied_bytes": sum(len(data) for data in files.values()),
        "changed_files": len(changes),
    }


def _parse_record(old_from_git: str, new_from_git: str, record: list[str]) -> ReviewChange:
    rename_from: str | None = None
    rename_to: str | None = None
    old_header: str | None = None
    new_header: str | None = None
    has_file_headers = False
    position = 0
    while position < len(record) and not record[position].startswith("@@ "):
        line = record[position]
        if line.startswith("rename from "):
            if rename_from is not None:
                raise _policy("diff has malformed rename headers")
            rename_from = _safe_path(_extended_path(line.removeprefix("rename from ")))
        elif line.startswith("rename to "):
            if rename_to is not None:
                raise _policy("diff has malformed rename headers")
            rename_to = _safe_path(_extended_path(line.removeprefix("rename to ")))
        elif line.startswith("--- "):
            if old_header is not None or position + 1 >= len(record) or not record[position + 1].startswith("+++ "):
                raise _policy("diff has malformed file headers")
            old_header = _header_path(record[position][4:], "a")
            new_header = _header_path(record[position + 1][4:], "b")
            has_file_headers = True
            position += 1
        elif line.startswith(("+++ ", "---")):
            raise _policy("diff has malformed file headers")
        elif line.startswith(("index ", "old mode ", "new mode ", "new file mode ", "deleted file mode ", "similarity index ", "dissimilarity index ")):
            pass
        elif not line:
            raise _policy("diff has malformed file record")
        else:
            raise _policy("diff has malformed file record")
        position += 1

    if (rename_from is None) != (rename_to is None):
        raise _policy("diff has malformed rename headers")
    if rename_from is not None:
        if rename_from != old_from_git or rename_to != new_from_git:
            raise _policy("diff rename paths do not match file header")
    if not has_file_headers:
        if rename_from is None or position != len(record):
            raise _policy("diff has no changes")
        if rename_from == rename_to:
            raise _policy("diff has no changes")
        return ReviewChange(new_from_git, (), False)

    deleted = new_header is None
    if (old_header is not None and old_header != old_from_git) or (new_header is not None and new_header != new_from_git):
        raise _policy("diff paths do not match file header")
    if old_header is None and new_header is None:
        raise _policy("diff has malformed file headers")
    if rename_from is not None and (old_header != rename_from or new_header != rename_to):
        raise _policy("diff rename paths do not match file header")

    added: list[int] = []
    saw_hunk = False
    saw_content_change = False
    while position < len(record):
        line = record[position]
        if not line.startswith("@@ "):
            raise _policy("diff has malformed hunk header")
        old_start, old_count, new_start, new_count = _hunk_header(line)
        if old_header is None and (old_start != 0 or old_count != 0):
            raise _policy("added file has malformed hunk range")
        if deleted and (new_start != 0 or new_count != 0):
            raise _policy("deleted file has malformed hunk range")
        position += 1
        old_seen = 0
        new_seen = 0
        previous_was_content = False
        while position < len(record) and not record[position].startswith("@@ "):
            body = record[position]
            if body.startswith("+"):
                new_seen += 1
                added.append(new_start + new_seen - 1)
                previous_was_content = True
                saw_content_change = True
            elif body.startswith("-"):
                old_seen += 1
                previous_was_content = True
                saw_content_change = True
            elif body.startswith(" "):
                old_seen += 1
                new_seen += 1
                previous_was_content = True
            elif body == "\\ No newline at end of file" and previous_was_content:
                previous_was_content = False
            else:
                raise _policy("diff has malformed hunk content")
            position += 1
        if old_seen != old_count or new_seen != new_count:
            raise _policy("diff hunk line counts do not match header")
        saw_hunk = True
    if not saw_hunk or not saw_content_change:
        raise _policy("diff has no changes")
    target = old_header if deleted else new_header
    if target is None:
        raise _policy("diff has malformed file headers")
    return ReviewChange(target, tuple(sorted(set(added))), deleted)


def _git_paths(line: str, record: list[str]) -> tuple[str, str]:
    prefix = "diff --git "
    if not line.startswith(prefix):
        raise _policy("diff has malformed file header")
    fields = line.removeprefix(prefix)
    if fields.startswith('"'):
        old, remainder = _quoted_path(fields)
        if not remainder.startswith(" "):
            raise _policy("diff has malformed file header")
        new, remainder = _path_token(remainder[1:])
        if remainder:
            raise _policy("diff has malformed file header")
    else:
        candidates = _unquoted_header_candidates(fields)
        declared = _declared_record_paths(record)
        if declared:
            candidates = [candidate for candidate in candidates if all(_matches_declared_path(candidate, pair) for pair in declared)]
        if len(candidates) != 1:
            raise _policy("diff has malformed file header")
        return candidates[0]
    if not old.startswith("a/") or not new.startswith("b/"):
        raise _policy("diff has malformed file header")
    return _safe_path(old[2:]), _safe_path(new[2:])


def _unquoted_header_candidates(fields: str) -> list[tuple[str, str]]:
    candidates: set[tuple[str, str]] = set()
    for marker in (" b/", ' "b/'):
        position = fields.find(marker)
        while position > 0:
            old = fields[:position]
            try:
                new, remainder = _path_token(fields[position + 1 :])
                if not remainder and old.startswith("a/") and new.startswith("b/"):
                    candidates.add((_safe_path(old[2:]), _safe_path(new[2:])))
            except TargetError:
                pass
            position = fields.find(marker, position + 1)
    return sorted(candidates)


def _declared_record_paths(record: list[str]) -> list[tuple[str | None, str | None]]:
    declared: list[tuple[str | None, str | None]] = []
    rename_from: str | None = None
    rename_to: str | None = None
    for position, line in enumerate(record):
        if line.startswith("@@ "):
            break
        if line.startswith("rename from "):
            rename_from = _safe_path(_extended_path(line.removeprefix("rename from ")))
        elif line.startswith("rename to "):
            rename_to = _safe_path(_extended_path(line.removeprefix("rename to ")))
        elif line.startswith("--- ") and position + 1 < len(record) and record[position + 1].startswith("+++ "):
            declared.append((_header_path(line[4:], "a"), _header_path(record[position + 1][4:], "b")))
    if rename_from is not None and rename_to is not None:
        declared.append((rename_from, rename_to))
    return declared


def _matches_declared_path(candidate: tuple[str, str], declared: tuple[str | None, str | None]) -> bool:
    old, new = declared
    return (old is None or old == candidate[0]) and (new is None or new == candidate[1])


def _header_path(value: str, prefix: str) -> str | None:
    path, remainder = _path_token(value)
    if remainder and not remainder.startswith("\t"):
        raise _policy("diff has malformed file headers")
    if path == "/dev/null":
        return None
    if not path.startswith(prefix + "/"):
        raise _policy("diff has malformed file headers")
    return _safe_path(path[2:])


def _safe_path(value: str) -> str:
    if not value or "\x00" in value or "\\" in value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _policy("diff path is unsafe")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise _policy("diff path is unsafe")
    return path.as_posix()


def _hunk_header(line: str) -> tuple[int, int, int, int]:
    match = _HUNK.fullmatch(line)
    if match is None:
        raise _policy("diff has malformed hunk header")
    old_start = int(match["old_start"])
    old_count = int(match["old_count"] or "1")
    new_start = int(match["new_start"])
    new_count = int(match["new_count"] or "1")
    if (old_count and old_start < 1) or (new_count and new_start < 1):
        raise _policy("diff has malformed hunk header")
    return old_start, old_count, new_start, new_count


def _path_token(value: str) -> tuple[str, str]:
    if value.startswith('"'):
        return _quoted_path(value)
    path, separator, remainder = value.partition("\t")
    return path, separator + remainder


def _extended_path(value: str) -> str:
    path, remainder = _path_token(value)
    if remainder:
        raise _policy("diff has malformed rename headers")
    return path


def _quoted_path(value: str) -> tuple[str, str]:
    encoded = bytearray()
    position = 1
    escapes = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, '"': 34, "\\": 92}
    while position < len(value):
        character = value[position]
        if character == '"':
            try:
                return bytes(encoded).decode("utf-8"), value[position + 1 :]
            except UnicodeDecodeError as error:
                raise _policy("diff has malformed quoted path") from error
        if character != "\\":
            encoded.extend(character.encode("utf-8"))
            position += 1
            continue
        position += 1
        if position >= len(value):
            break
        escaped = value[position]
        if escaped in escapes:
            encoded.append(escapes[escaped])
            position += 1
            continue
        if escaped in "01234567" and position + 2 < len(value) and all(item in "01234567" for item in value[position : position + 3]):
            encoded.append(int(value[position : position + 3], 8))
            position += 3
            continue
        raise _policy("diff has malformed quoted path")
    raise _policy("diff has malformed quoted path")


def _read_diff(path: Path) -> str:
    data = _read_regular(path, "diff")
    if not data or len(data) > _MAX_DIFF_BYTES or b"\x00" in data:
        raise _policy("diff is empty, binary or oversized")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _policy("diff must be UTF-8") from error


def _read_filtered_checkout(root: Path) -> dict[str, bytes]:
    if root.is_symlink() or not root.is_dir():
        raise _policy("source checkout is unavailable")
    files: dict[str, bytes] = {}
    total = 0
    try:
        for current, directories, names in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            directories[:] = [name for name in directories if name not in _SKIPPED_DIRECTORIES]
            for name in directories:
                entry = current_path / name
                if entry.is_symlink():
                    raise _policy("source checkout contains a symlink")
            for name in sorted(names):
                entry = current_path / name
                relative = entry.relative_to(root).as_posix()
                if entry.is_symlink():
                    raise _policy("source checkout contains a symlink")
                _reject_credential(relative)
                if entry.suffix not in _ALLOWED_SUFFIXES:
                    continue
                file_stat = entry.stat()
                if not stat.S_ISREG(file_stat.st_mode):
                    raise _policy("source checkout allowed entry is not a regular file")
                data = entry.read_bytes()
                if len(data) > _MAX_FILE_BYTES:
                    raise _policy("source checkout file is oversized")
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise _policy("source checkout file is not UTF-8") from error
                if len(files) >= _MAX_FILES:
                    raise _policy("source checkout has too many files")
                total += len(data)
                if total > _MAX_TOTAL_BYTES:
                    raise _policy("source checkout is too large")
                files[relative] = data
    except TargetError:
        raise
    except OSError as error:
        raise _policy("unable to read source checkout") from error
    return files


def _validate_changed_targets(changes: tuple[ReviewChange, ...], root: Path) -> None:
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise _policy("source checkout is unavailable") from error
    for change in changes:
        if change.deleted:
            continue
        target = root.joinpath(*PurePosixPath(change.path).parts)
        try:
            ancestor = root
            for part in PurePosixPath(change.path).parts:
                ancestor = ancestor / part
                if stat.S_ISLNK(ancestor.lstat().st_mode):
                    raise _policy("changed target has a symlink ancestor")
            target_stat = target.lstat()
            target.resolve(strict=True).relative_to(resolved_root)
        except (OSError, ValueError) as error:
            raise _policy("changed target is unavailable") from error
        if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode):
            raise _policy("changed target is not a regular file")


def _read_regular(path: Path, label: str) -> bytes:
    try:
        file_stat = path.lstat()
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise _policy(f"{label} is unavailable")
        return path.read_bytes()
    except TargetError:
        raise
    except OSError as error:
        raise _policy(f"unable to read {label}") from error


def _reject_credential(relative: str) -> None:
    name = PurePosixPath(relative).name
    if name == ".env" or name.endswith(".pem") or name.endswith(".key"):
        raise _policy("source checkout contains a credential file")


def _write_tree(root: Path, files: dict[str, bytes]) -> None:
    for relative, data in files.items():
        path = PurePosixPath(relative)
        target = root.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _policy(message: str) -> TargetError:
    return TargetError(ErrorCode.POLICY, message)


__all__ = ["ReviewChange", "parse_unified_diff", "prepare_review"]
