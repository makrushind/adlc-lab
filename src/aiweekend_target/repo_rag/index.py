"""Deterministically build the SQLite FTS index used by repo-rag."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from aiweekend_target.errors import ErrorCode, TargetError

CHUNK_LINES = 40
CHUNK_OVERLAP = 10
MAX_CHUNK_BYTES = 8 * 1024
# Agent queries are capped at 256 UTF-8 bytes, so a crossing token needs at most the preceding 255 bytes.
MAX_SEARCH_TOKEN_BYTES = 256
CHUNK_BYTE_OVERLAP = MAX_SEARCH_TOKEN_BYTES - 1


def build_index(corpus_root: str | Path, database_path: str | Path) -> None:
    """Atomically replace *database_path* with an index of *corpus_root*."""
    root = Path(corpus_root)
    target = Path(database_path)
    try:
        _validate_corpus_root(root)
        target.parent.mkdir(parents=True, exist_ok=True)
    except TargetError:
        raise
    except OSError as error:
        raise TargetError(ErrorCode.MCP, "unable to prepare repository index") from error
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, delete=False
        ) as temporary:
            temporary_name = temporary.name
        connection = sqlite3.connect(temporary_name)
        try:
            connection.execute(
                "CREATE VIRTUAL TABLE chunks USING fts5("
                "path UNINDEXED, line_start UNINDEXED, line_end UNINDEXED, content)"
            )
            for source in _source_files(root):
                relative = source.relative_to(root).as_posix()
                try:
                    lines = source.read_text(encoding="utf-8").splitlines()
                except UnicodeDecodeError as error:
                    raise TargetError(ErrorCode.MCP, "corpus file is not UTF-8", {"path": relative}) from error
                for line_start, line_end, content in _chunks(lines):
                    connection.execute(
                        "INSERT INTO chunks(path, line_start, line_end, content) VALUES (?, ?, ?, ?)",
                        (relative, line_start, line_end, content),
                    )
            connection.commit()
        finally:
            connection.close()
        os.replace(temporary_name, target)
        temporary_name = None
    except TargetError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise TargetError(ErrorCode.MCP, "unable to rebuild repository index") from error
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def _validate_corpus_root(root: Path) -> None:
    if not root.is_dir():
        raise TargetError(ErrorCode.MCP, "corpus root is not a directory")
    if any(ancestor.is_symlink() for ancestor in (root, *root.parents)):
        raise TargetError(ErrorCode.POLICY, "corpus root must not be a symlink")


def _source_files(root: Path) -> list[Path]:
    sources: list[Path] = []
    resolved_root = root.resolve(strict=True)
    for source in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if source.is_symlink():
            raise TargetError(ErrorCode.POLICY, "corpus entries must not be symlinks", {"path": str(source)})
        if source.is_file():
            try:
                source.resolve(strict=True).relative_to(resolved_root)
            except ValueError as error:
                raise TargetError(ErrorCode.POLICY, "corpus entry escapes its root", {"path": str(source)}) from error
            sources.append(source)
    return sources


def _chunks(lines: list[str]) -> list[tuple[int, int, str]]:
    if not lines:
        return []
    step = CHUNK_LINES - CHUNK_OVERLAP
    chunks: list[tuple[int, int, str]] = []
    for start in range(0, len(lines), step):
        if chunks and chunks[-1][1] == len(lines):
            break
        window = lines[start : start + CHUNK_LINES]
        content = "\n".join(window)
        if len(content.encode("utf-8")) <= MAX_CHUNK_BYTES:
            chunks.append((start + 1, start + len(window), content))
        else:
            chunks.extend(_bounded_window(window, start + 1))
    return chunks


def _bounded_window(lines: list[str], first_line: int) -> list[tuple[int, int, str]]:
    chunks: list[tuple[int, int, str]] = []
    pending: list[str] = []
    pending_bytes = 0
    pending_start = first_line

    def flush(line_end: int) -> None:
        nonlocal pending, pending_bytes
        if pending:
            chunks.append((pending_start, line_end, "\n".join(pending)))
            pending = []
            pending_bytes = 0

    for offset, line in enumerate(lines):
        line_number = first_line + offset
        line_bytes = len(line.encode("utf-8"))
        if line_bytes > MAX_CHUNK_BYTES:
            flush(line_number - 1)
            chunks.extend((line_number, line_number, part) for part in _split_utf8(line))
            pending_start = line_number + 1
            continue
        added_bytes = line_bytes + (1 if pending else 0)
        if pending and pending_bytes + added_bytes > MAX_CHUNK_BYTES:
            flush(line_number - 1)
            pending_start = line_number
            added_bytes = line_bytes
        elif not pending:
            pending_start = line_number
        pending.append(line)
        pending_bytes += added_bytes
    flush(first_line + len(lines) - 1)
    return chunks


def _split_utf8(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    end = 0
    size = 0
    while end < len(value):
        character = value[end]
        character_size = len(character.encode("utf-8"))
        if end > start and size + character_size > MAX_CHUNK_BYTES:
            parts.append(value[start:end])
            overlap_start = end
            overlap_size = 0
            while overlap_start > start and overlap_size < CHUNK_BYTE_OVERLAP:
                overlap_start -= 1
                overlap_size += len(value[overlap_start].encode("utf-8"))
            start = overlap_start
            size = overlap_size
            continue
        size += character_size
        end += 1
    if end > start:
        parts.append(value[start:end])
    return parts
