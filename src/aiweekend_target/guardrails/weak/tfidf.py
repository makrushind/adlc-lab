"""Dependency-free TF-IDF cosine similarity over the packaged detector examples."""

from __future__ import annotations

import re
from collections import Counter
from math import log, sqrt


def tokens(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[\w]+", text.lower(), re.UNICODE))


def _vector(
    text: str, document_frequency: Counter[str], document_count: int
) -> dict[str, float]:
    counts = Counter(tokens(text))
    return {
        term: count * (log((1 + document_count) / (1 + document_frequency[term])) + 1)
        for term, count in counts.items()
    }


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    dot = sum(weight * right.get(term, 0.0) for term, weight in left.items())
    left_norm = sqrt(sum(weight * weight for weight in left.values()))
    right_norm = sqrt(sum(weight * weight for weight in right.values()))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def nearest(
    text: str, attack: tuple[str, ...], benign: tuple[str, ...]
) -> tuple[float, float]:
    catalogue = attack + benign
    frequency = Counter(term for item in catalogue for term in set(tokens(item)))
    query = _vector(text, frequency, len(catalogue))
    attack_scores = [
        _cosine(query, _vector(item, frequency, len(catalogue))) for item in attack
    ]
    benign_scores = [
        _cosine(query, _vector(item, frequency, len(catalogue))) for item in benign
    ]
    return max(attack_scores, default=0.0), max(benign_scores, default=0.0)
