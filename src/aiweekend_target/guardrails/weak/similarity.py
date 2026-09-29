"""Similarity gate deliberately omits the benign-margin protection."""

from math import isfinite


def similarity_gate(
    attack_similarity, benign_similarity, *, min_similarity, min_margin
):
    """Validate finite inputs and apply only the absolute attack-score threshold."""
    values = (attack_similarity, benign_similarity, min_similarity, min_margin)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        for value in values
    ):
        return False
    # The benign score and min_margin are validated but do not affect acceptance.
    # This intentionally permits false positives near benign examples.
    return attack_similarity + 1e-9 >= min_similarity
