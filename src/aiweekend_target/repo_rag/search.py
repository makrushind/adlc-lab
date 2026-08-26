"""Safe public query API for the repo-rag SQLite index."""

from __future__ import annotations

import fnmatch
import os
import re
import sqlite3
from contextlib import closing
from pathlib import PurePosixPath
from aiweekend_target.errors import ErrorCode, TargetError
from aiweekend_target.repo_rag.types import SearchResponse

DATABASE_ENV = "AIWEEKEND_REPO_RAG_DB"
_TOKEN = re.compile(r"\w+", re.UNICODE)


class RepoSearch:
    """A read-only search facade over one explicitly supplied database."""

    def __init__(self, database_path: str | os.PathLike[str]) -> None:
        self.database_path = os.fspath(database_path)

    def search_repo(self, query: str, limit: int = 5, path_glob: str | None = None) -> SearchResponse:
        _validate_arguments(query, limit, path_glob)
        expression = _query_expression(query)
        if not expression:
            return {"results": []}
        try:
            with closing(sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True)) as connection:
                rows = connection.execute(
                    "SELECT path, line_start, line_end, content FROM chunks "
                    "WHERE chunks MATCH ? ORDER BY bm25(chunks) ASC, path ASC, line_start ASC",
                    (expression,),
                ).fetchall()
        except sqlite3.Error as error:
            raise TargetError(ErrorCode.MCP, "repository index is unavailable") from error
        results = [
            {"path": path, "line_start": line_start, "line_end": line_end, "content": content}
            for path, line_start, line_end, content in rows
            if path_glob is None or fnmatch.fnmatchcase(path, path_glob)
        ]
        return {"results": results[:limit]}


def search_repo(query: str, limit: int = 5, path_glob: str | None = None) -> SearchResponse:
    """Search the database named by ``AIWEEKEND_REPO_RAG_DB``."""
    database_path = os.environ.get(DATABASE_ENV)
    if not database_path:
        raise TargetError(ErrorCode.MCP, "repository index path is not configured")
    return RepoSearch(database_path).search_repo(query, limit, path_glob)


def _validate_arguments(query: object, limit: object, path_glob: object) -> None:
    if not isinstance(query, str) or not query.strip():
        raise TargetError(ErrorCode.POLICY, "query must be non-empty text")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise TargetError(ErrorCode.POLICY, "limit must be an integer from 1 through 20")
    if path_glob is None:
        return
    if not isinstance(path_glob, str) or not path_glob or "\x00" in path_glob or "\\" in path_glob:
        raise TargetError(ErrorCode.POLICY, "path_glob must be a safe POSIX-relative glob")
    path = PurePosixPath(path_glob)
    if path.is_absolute() or ".." in path.parts:
        raise TargetError(ErrorCode.POLICY, "path_glob must be a safe POSIX-relative glob")


def _query_expression(query: str) -> str:
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in _TOKEN.findall(query))
