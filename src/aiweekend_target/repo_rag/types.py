"""Shared, checked shapes for repository-search tool responses."""

from __future__ import annotations

from typing import TypedDict


class SearchResult(TypedDict):
    path: str
    line_start: int
    line_end: int
    content: str


class SearchResponse(TypedDict):
    results: list[SearchResult]
