"""Comp-tool keyword matcher (whole-word) for richness scoring + lead-magnet hints.

Names the benchmarking / comp tools Pave displaces. Whole-word matching avoids
substring false positives (e.g. 'compa' inside 'company'). This is domain knowledge
for grading a comment's signal — it is NOT the scrape target list (that stays in
config/targets.json).
"""
from __future__ import annotations

import re

COMP_TOOLS = [
    # benchmarking / survey providers
    "radford", "mercer", "payscale", "companalyst", "willis towers watson", "wtw",
    "levels.fyi", "carta total comp", "ravio", "figures", "barley", "compa",
    "opencomp", "aeqium", "bettercomp", "candoriq", "pave",
    # comp planning / pay equity
    "workday comp", "beqom", "syndio", "trusaic",
    # the manual baseline (a strong "doing comp the hard way" signal)
    "spreadsheet", "spreadsheets", "excel", "google sheets",
]

_PATTERNS = [(t, re.compile(rf"\b{re.escape(t)}\b", re.I)) for t in COMP_TOOLS]


def matched_comp_tools(text: str) -> list[str]:
    """Which comp tools the text names, whole-word and case-insensitive."""
    low = text or ""
    return sorted({t for t, rx in _PATTERNS if rx.search(low)})
