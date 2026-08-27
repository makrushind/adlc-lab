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
_MAX_MARKER_BYTES = 16 * 1024
_ALLOWED_SUFFIXES = frozenset({".py", ".pyi", ".md", ".toml", ".yaml", ".yml", ".json", ".txt"})
_SKIPPED_DIRECTORIES = frozenset({
    ".git", ".venv", "venv", "env", "node_modules", "vendor", "build", "dist", "cache", ".cache",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
})
_HUNK = re.compile(r"^@@ -(?P<old_start>0|[1-9][0-9]*)(?:,(?P<old_count>[0-9]+))? \+(?P<new_start>0|[1-9][0-9]*)(?:,(?P<new_count>[0-9]+))? @@(?: .*)?$")
_INDEX = re.compile(r"^index (?P<old>[0-9a-f]{7,64})\.\.(?P<new>[0-9a-f]{7,64})(?: (?P<mode>[0-7]{6}))?$")
_MODE = re.compile(r"^[0-7]{6}$")


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
        _reject_credential(old_from_git)
        _reject_credential(new_from_git)
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
    diff_source = Path(diff_path)
    _validate_disjoint_roots(paths, source, diff_source)
    diff = _read_diff(diff_source)
    changes = parse_unified_diff(diff)
    files = _read_filtered_checkout(source)
    _validate_changed_targets(changes, source, files)
    marker = _read_regular(Path(baseline_marker), "baseline marker", _MAX_MARKER_BYTES)

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
    old_mode: str | None = None
    new_mode: str | None = None
    new_file_mode: str | None = None
    deleted_file_mode: str | None = None
    index_hashes: tuple[str, str] | None = None
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
        elif line.startswith("index "):
            if index_hashes is not None:
                raise _policy("diff has contradictory metadata")
            match = _INDEX.fullmatch(line)
            if match is None:
                raise _policy("diff has malformed metadata")
            index_hashes = (match["old"], match["new"])
        elif line.startswith("old mode "):
            old_mode = _record_mode(old_mode, line.removeprefix("old mode "))
        elif line.startswith("new mode "):
            new_mode = _record_mode(new_mode, line.removeprefix("new mode "))
        elif line.startswith("new file mode "):
            new_file_mode = _record_mode(new_file_mode, line.removeprefix("new file mode "))
        elif line.startswith("deleted file mode "):
            deleted_file_mode = _record_mode(deleted_file_mode, line.removeprefix("deleted file mode "))
        elif line.startswith(("similarity index ", "dissimilarity index ")):
            _validate_percentage(line.rsplit(" ", 1)[-1])
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
    if (old_mode is None) != (new_mode is None) or old_mode is not None and old_mode == new_mode:
        raise _policy("diff has contradictory metadata")
    if new_file_mode is not None and deleted_file_mode is not None:
        raise _policy("diff has contradictory metadata")
    if (new_file_mode is not None or deleted_file_mode is not None) and old_mode is not None:
        raise _policy("diff has contradictory metadata")
    if not has_file_headers:
        if position != len(record):
            raise _policy("diff has malformed file record")
        if rename_from is not None:
            if new_file_mode is not None or deleted_file_mode is not None or rename_from == rename_to:
                raise _policy("diff has contradictory metadata")
            return ReviewChange(new_from_git, (), False)
        if old_mode is not None:
            if old_from_git != new_from_git or index_hashes is not None:
                raise _policy("diff has contradictory metadata")
            return ReviewChange(new_from_git, (), False)
        if new_file_mode is not None:
            if old_from_git != new_from_git or not _empty_file_index(index_hashes, added=True):
                raise _policy("diff has contradictory metadata")
            return ReviewChange(new_from_git, (), False)
        if deleted_file_mode is not None:
            if old_from_git != new_from_git or not _empty_file_index(index_hashes, added=False):
                raise _policy("diff has contradictory metadata")
            return ReviewChange(old_from_git, (), True)
        raise _policy("diff has no changes")

    deleted = new_header is None
    if (old_header is not None and old_header != old_from_git) or (new_header is not None and new_header != new_from_git):
        raise _policy("diff paths do not match file header")
    if old_header is None and new_header is None:
        raise _policy("diff has malformed file headers")
    if rename_from is not None and (old_header != rename_from or new_header != rename_to):
        raise _policy("diff rename paths do not match file header")
    if new_file_mode is not None and old_header is not None:
        raise _policy("diff has contradictory metadata")
    if deleted_file_mode is not None and new_header is not None:
        raise _policy("diff has contradictory metadata")

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


def _record_mode(current: str | None, value: str) -> str:
    if current is not None or _MODE.fullmatch(value) is None:
        raise _policy("diff has malformed metadata")
    return value


def _validate_percentage(value: str) -> None:
    if not value.endswith("%") or not value[:-1].isdigit() or not 0 <= int(value[:-1]) <= 100:
        raise _policy("diff has malformed metadata")


def _empty_file_index(index_hashes: tuple[str, str] | None, *, added: bool) -> bool:
    if index_hashes is None:
        return False
    old_hash, new_hash = index_hashes
    return (not old_hash.strip("0") and bool(new_hash.strip("0"))) if added else (bool(old_hash.strip("0")) and not new_hash.strip("0"))


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
    data = _read_regular(path, "diff", _MAX_DIFF_BYTES)
    if not data or len(data) > _MAX_DIFF_BYTES or b"\x00" in data:
        raise _policy("diff is empty, binary or oversized")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _policy("diff must be UTF-8") from error


def _read_filtered_checkout(root: Path) -> dict[str, bytes]:
    try:
        root_stat = root.lstat()
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise _policy("source checkout is unavailable")
        resolved_root = root.resolve(strict=True)
        if _directory_identity(resolved_root.lstat()) != _directory_identity(root_stat):
            raise _policy("source checkout is unavailable")
    except TargetError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise _policy("source checkout is unavailable") from error
    files: dict[str, bytes] = {}
    total = 0

    def fail_walk(_: OSError) -> None:
        raise _policy("unable to read source checkout")

    try:
        for current, directories, names in os.walk(
            resolved_root,
            topdown=True,
            onerror=fail_walk,
            followlinks=False,
        ):
            current_path = Path(current)
            _verify_contained_directory(current_path, resolved_root, _directory_identity(root_stat))
            directories[:] = sorted(name for name in directories if name not in _SKIPPED_DIRECTORIES)
            for name in directories:
                entry = current_path / name
                if entry.is_symlink():
                    raise _policy("source checkout contains a symlink")
            for name in sorted(names):
                entry = current_path / name
                relative = entry.relative_to(resolved_root).as_posix()
                if entry.is_symlink():
                    raise _policy("source checkout contains a symlink")
                _reject_credential(relative)
                if entry.suffix not in _ALLOWED_SUFFIXES:
                    continue
                if len(files) >= _MAX_FILES:
                    raise _policy("source checkout has too many files")
                data = _read_regular(
                    entry,
                    "source checkout file",
                    _MAX_FILE_BYTES,
                    containment_root=resolved_root,
                    root_identity=_directory_identity(root_stat),
                )
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise _policy("source checkout file is not UTF-8") from error
                total += len(data)
                if total > _MAX_TOTAL_BYTES:
                    raise _policy("source checkout is too large")
                files[relative] = data
        _verify_contained_directory(resolved_root, resolved_root, _directory_identity(root_stat))
    except TargetError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise _policy("unable to read source checkout") from error
    return files


def _validate_changed_targets(
    changes: tuple[ReviewChange, ...],
    root: Path,
    files: dict[str, bytes],
) -> None:
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise _policy("source checkout is unavailable") from error
    for change in changes:
        if change.deleted:
            continue
        relative = PurePosixPath(change.path)
        if change.path.endswith(".py") and any(part in _SKIPPED_DIRECTORIES for part in relative.parts[:-1]):
            raise _policy("changed Python target is excluded from the review corpus")
        if change.path.endswith(".py") and change.path not in files:
            raise _policy("changed Python target is excluded from the review corpus")
        target = root.joinpath(*relative.parts)
        try:
            ancestor = root
            for part in relative.parts:
                ancestor = ancestor / part
                if stat.S_ISLNK(ancestor.lstat().st_mode):
                    raise _policy("changed target has a symlink ancestor")
            target_stat = target.lstat()
            target.resolve(strict=True).relative_to(resolved_root)
        except (OSError, ValueError) as error:
            raise _policy("changed target is unavailable") from error
        if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode):
            raise _policy("changed target is not a regular file")


def _read_regular(
    path: Path,
    label: str,
    maximum: int,
    *,
    containment_root: Path | None = None,
    root_identity: tuple[int, int, int] | None = None,
) -> bytes:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise _policy(f"{label} is unavailable")
        if before.st_size > maximum:
            raise _policy(f"{label} is oversized")
        ancestors = _ancestor_snapshots(path, containment_root, root_identity, label)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(os.fspath(path), flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(before):
                raise _policy(f"{label} changed while being read")
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            if _file_identity(after) != _file_identity(opened):
                raise _policy(f"{label} changed while being read")
        finally:
            os.close(descriptor)
        path_after = path.lstat()
        if _file_identity(path_after) != _file_identity(before):
            raise _policy(f"{label} changed while being read")
        _verify_ancestor_snapshots(ancestors, label)
        data = b"".join(chunks)
        if len(data) > maximum or len(data) != before.st_size:
            raise _policy(f"{label} is oversized or changed while being read")
        return data
    except TargetError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise _policy(f"unable to read {label}") from error


def _ancestor_snapshots(
    path: Path,
    root: Path | None,
    root_identity: tuple[int, int, int] | None,
    label: str,
) -> tuple[tuple[Path, tuple[int, int, int]], ...]:
    if root is None:
        return ()
    try:
        relative = path.relative_to(root)
        path.resolve(strict=True).relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise _policy(f"{label} is unavailable") from error
    snapshots: list[tuple[Path, tuple[int, int, int]]] = []
    current = root
    for part in (None, *relative.parts[:-1]):
        if part is not None:
            current = current / part
        current_stat = current.lstat()
        identity = _directory_identity(current_stat)
        if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
            raise _policy(f"{label} has an unsafe path")
        if current == root and root_identity is not None and identity != root_identity:
            raise _policy(f"{label} has an unsafe path")
        snapshots.append((current, identity))
    return tuple(snapshots)


def _verify_ancestor_snapshots(
    snapshots: tuple[tuple[Path, tuple[int, int, int]], ...],
    label: str,
) -> None:
    for path, identity in snapshots:
        current = path.lstat()
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode) or _directory_identity(current) != identity:
            raise _policy(f"{label} changed while being read")


def _verify_contained_directory(
    path: Path,
    root: Path,
    root_identity: tuple[int, int, int],
) -> None:
    try:
        relative = path.relative_to(root)
        path.resolve(strict=True).relative_to(root)
        current = root
        for part in (None, *relative.parts):
            if part is not None:
                current = current / part
            current_stat = current.lstat()
            identity = _directory_identity(current_stat)
            if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
                raise _policy("source checkout has an unsafe path")
            if current == root and identity != root_identity:
                raise _policy("source checkout has an unsafe path")
    except TargetError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise _policy("source checkout has an unsafe path") from error


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns


def _directory_identity(value: os.stat_result) -> tuple[int, int, int]:
    return value.st_dev, value.st_ino, value.st_mode


def _validate_disjoint_roots(paths: LabPaths, source: Path, diff: Path) -> None:
    try:
        protected = (source.resolve(strict=True), diff.resolve(strict=True))
        destinations = tuple(
            Path(destination).resolve(strict=False)
            for destination in (paths.workspace, paths.corpus, paths.rag_index)
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise _policy("review preparation roots are unavailable") from error
    if any(_paths_overlap(destination, item) for destination in destinations for item in protected):
        raise _policy("review preparation roots overlap")
    for position, destination in enumerate(destinations):
        if any(_paths_overlap(destination, other) for other in destinations[position + 1 :]):
            raise _policy("review preparation roots overlap")


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _reject_credential(relative: str) -> None:
    name = PurePosixPath(relative).name
    if name == ".env" or name.endswith(".pem") or name.endswith(".key"):
        raise _policy("review input contains a credential path")


def _write_tree(root: Path, files: dict[str, bytes]) -> None:
    for relative, data in files.items():
        path = PurePosixPath(relative)
        target = root.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _policy(message: str) -> TargetError:
    return TargetError(ErrorCode.POLICY, message)


__all__ = ["ReviewChange", "parse_unified_diff", "prepare_review"]
