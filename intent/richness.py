"""Signal-richness: how much a lead's comment actually tells a rep.

The gate requires verbatim evidence; this grades HOW MUCH that evidence says. A
one-word hand-raise ("Interested!") proves intent but carries no context; a
substantive comment ("we're ripping out the Radford spreadsheet, what's everyone
benchmarking with?") names the tool and the pain. Reps should see the rich ones
first. Deterministic, so it runs zero-cred and is testable.
"""
from __future__ import annotations

from .comp_tools import matched_comp_tools

_NEED_SIGNALS = [
    "need", "looking for", "wish", "problem", "limited", "frustrat", "switch", "instead",
    "compared", " vs ", "versus", "trying", "evaluat", "struggle", "can't", "cannot",
    "doesn't", "does not", "however", "alternative", "prefer", "better than", "worse",
    "too expensive", "pricing", "budget", "migrat", "benchmark", "merit cycle",
    "pay band", "salary band", "comp cycle", "total rewards", "pay equity",
]


def score_richness(comment: str) -> tuple[int, str]:
    """Return (0-3 score, label). thin = bare hand-raise, rich = carries real context."""
    text = comment or ""
    low = text.lower()
    score = 0
    if len(text.split()) >= 12:                       # substantive length
        score += 1
    if matched_comp_tools(text):                      # names a specific comp tool
        score += 1
    if "?" in text or any(s in low for s in _NEED_SIGNALS):  # states a need/pain/comparison
        score += 1
    label = "thin" if score == 0 else ("moderate" if score == 1 else "rich")
    return score, label
