"""ICP title filter for the wide-net track — a faithful mirror of the scrape's filter.

The wide-net retriever pulls commenters from posts the scrape never touched, so they
need the SAME ICP gate the curated track applies in scripts/scrape.py. To avoid any
risk to the proven scrape, this module REPLICATES that logic (same DEFAULT_ICP, same
config/targets.json `icp` override block, same regexes) rather than importing it across
the scripts/ boundary. tests/test_intent.py asserts byte-for-byte parity with
scrape.py so the two can never drift. This is the existing filter — NOT the Figma
titles.py / CPO-flip, which is deliberately not used here.
"""
from __future__ import annotations

import json
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Mirrors scrape.DEFAULT_ICP exactly.
DEFAULT_ICP = {
    "tier1": (r"(\bchief\b|chief\s*\w+\s*officer|\bc[a-z]o\b|\bvp\b|vice\s*president|"
              r"\bhead\s*of\b|\bdirector\b)"),
    "tier2": (r"(\bmanager\b|\blead\b|\bsenior\b|\bprincipal\b|\bspecialist\b)"),
    "exclude": (r"\b(software\s*engineer|developer|data\s*scien\w*|data\s*entry|machine\s*learning|"
                r"designer|engineer|teacher|student|\bintern\b|recruiter|sourcer|"
                r"sales\s*development|account\s*executive|inside\s*sales|"
                r"sales\s*(executive|rep|representative|manager|lead|leader|director|operations)|"
                r"\bsdr\b|\bbdr\b|investor|venture|attorney|legal|professor|instructor|adjunct|"
                r"lecturer|physical\s*therapist|\bnurse\b|customer\s*success|coach)\b"),
}


def _load_icp_overrides() -> dict:
    path = os.path.join(REPO, "config", "targets.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f).get("icp", {}) or {}
    except (ValueError, OSError):
        return {}


_ICP = {**DEFAULT_ICP, **{k: v for k, v in _load_icp_overrides().items() if v}}
TIER1_RE = re.compile(_ICP["tier1"], re.I)
TIER2_RE = re.compile(_ICP["tier2"], re.I)
EXCLUDE_ICP_RE = re.compile(_ICP["exclude"], re.I)


def is_non_icp(title: str) -> bool:
    """Cheap drop on the noisy headline: a Tier-1/2 signal WINS over an incidental
    excluded word; otherwise an exclude hit drops the row."""
    t = title or ""
    if TIER1_RE.search(t) or TIER2_RE.search(t):
        return False
    return bool(EXCLUDE_ICP_RE.search(t))


def is_icp(title: str) -> bool:
    """KEEP gate: the title must positively match Tier 1 or Tier 2."""
    t = title or ""
    return bool(TIER1_RE.search(t) or TIER2_RE.search(t))


# --- Competitor / own-company exclusion (the wide-net analogue of the scrape's
# exclude_engager_companies + own_company_engager). A comp-vendor's own employees are
# not Pave buyers. The curated scrape already does this; wide-net pulls from untrusted
# posts, so it must too. -------------------------------------------------------------
from .comp_tools import COMP_TOOLS  # noqa: E402

# The manual baseline ("spreadsheet"/"excel") names tools, not employers — never an
# exclusion term for a person's company.
_MANUAL_BASELINE = {"spreadsheet", "spreadsheets", "excel", "google sheets"}


def _vendor_terms() -> set:
    terms = {t.lower() for t in COMP_TOOLS} - _MANUAL_BASELINE
    path = os.path.join(REPO, "config", "targets.json")
    try:
        with open(path) as f:
            eng = json.load(f).get("engagement", {})
        terms |= {x.lower() for x in eng.get("exclude_engager_companies", [])}
    except (ValueError, OSError):
        pass
    return terms


_VENDOR_TERMS = sorted(_vendor_terms(), key=len, reverse=True)
_VENDOR_RE = (re.compile("|".join(rf"\b{re.escape(t)}\b" for t in _VENDOR_TERMS), re.I)
              if _VENDOR_TERMS else None)


def is_competitor_employee(company: str | None, headline: str | None = "") -> bool:
    """True if the commenter works at a comp vendor / our own company (drop — not a buyer).
    Matches whole-word against the company field and headline (catches 'CEO at Bettercomp')."""
    if not _VENDOR_RE:
        return False
    return bool(_VENDOR_RE.search(f"{company or ''} {headline or ''}"))
