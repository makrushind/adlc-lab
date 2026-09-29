"""Narrow phrase and TF-IDF detector with deliberately limited coverage."""

import json
from importlib.resources import files

from .contracts import Signal
from .similarity import similarity_gate
from .tfidf import nearest


def narrow_signals(text: str) -> tuple[Signal, ...]:
    text = text.lower()
    rules = (
        ("ignore previous instructions", "PROMPT_OVERRIDE"),
        ("bypass moderation", "MODERATION_EVASION"),
        ("write a threat", "GENERATE_ABUSE"),
        ("reporter identity", "PRIVATE_DATA_REQUEST"),
    )
    found = [
        Signal(code, "joined", "weak") for phrase, code in rules if phrase in text
    ]
    catalog = json.loads(files(__package__).joinpath("prototypes.json").read_text())
    attack, benign = nearest(text, tuple(catalog["active"]), tuple(catalog["benign"]))
    if (
        similarity_gate(attack, benign, min_similarity=0.22, min_margin=0.0)
        and not found
    ):
        found.append(Signal("PROMPT_OVERRIDE", "joined", "weak", attack, benign))
    return tuple(found)
