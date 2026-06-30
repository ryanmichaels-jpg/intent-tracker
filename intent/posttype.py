"""POST stage: classify a post BEFORE judging its comments.

Two axes: structure (post_type — does commenting reveal comp-tooling intent?) and
Pave overlap (pave_surface — could Pave displace the use case?). For CURATED
competitor sources we trust the source and do NOT hard-drop on this gate; the caller
uses it to (a) condition the comment classifier ("the post is the prior") and
(b) downgrade a surface to review when the post doesn't qualify. The LLM judges via
the capability map; without ANTHROPIC_API_KEY the caller skips this stage entirely.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache

from .schema import QUALIFYING_POST_TYPES, PaveSurface, PostClassification, PostType

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POST_CLASSIFIER_MODEL = os.environ.get("POST_CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")
_CAPS = os.path.join(REPO, "config", "pave_capabilities.json")

_LEAD_MAGNET_CUES = [
    r"comment\b.{0,30}\bi'?ll (send|dm|share)",
    r"\btype\b\s+[\"'a-z]+\s+(below|in the comments)",
    r"comment\s+[\"'][a-z ]+[\"']",
    r"drop\s+a\s+comment.{0,30}(send|dm|guide|template)",
    r"dm\s+you\s+the\b",
    r"\bcomment\b.{0,20}\bbelow\b.{0,40}\b(guide|link|template|playbook|pdf|bands?)",
]


@lru_cache(maxsize=1)
def _capability_summary() -> str:
    with open(_CAPS, "r", encoding="utf-8") as fh:
        caps = json.load(fh)
    lines = [f"- {k}: {s['what']} (displaces: {', '.join(s['displaces'][:4])})"
             for k, s in caps["surfaces"].items()]
    not_pave = "; ".join(caps["not_pave"])
    return "PAVE SURFACES:\n" + "\n".join(lines) + f"\n\nNOT PAVE (pave_surface=none): {not_pave}"


def _system_prompt() -> str:
    return (
        "You triage LinkedIn posts as lead sources for Pave (compensation / total "
        "rewards). For each post decide:\n"
        "1) post_type: lead_magnet (offers an asset to comment for) / tool_question "
        "(asks what comp tools/survey providers people use) / tool_comparison "
        "(compares comp tools) / showcase (shows a result, no tool-choice ask) / off_topic.\n"
        "2) pave_surface: which Pave surface could DISPLACE the solution the poster is "
        "offering or discussing — i.e. could a comp/people leader do this in Pave "
        "instead? Use 'none' if Pave cannot solve this use case.\n"
        "Judge overlap by the USE CASE, not the word 'comp'. Benchmarking/salary survey "
        "-> benchmarking; merit/promotion cycle -> comp_planning; salary bands/leveling "
        "-> pay_bands; pay equity/transparency -> pay_equity; total rewards statements -> "
        "total_rewards; offers/comp letters -> offers. Payroll, recruiting/ATS, "
        "performance reviews, L&D, generic HRIS are NOT Pave (none).\n\n"
        + _capability_summary() + "\n\nReturn only JSON."
    )


_POST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["post_type", "pave_surface", "use_case", "tools_mentioned", "reason"],
    "properties": {
        "post_type": {"type": "string", "enum": [e.value for e in PostType]},
        "pave_surface": {"type": "string", "enum": [e.value for e in PaveSurface]},
        "use_case": {"type": "string", "description": "What the poster is offering/addressing, in a few words."},
        "tools_mentioned": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string", "description": "one short sentence"},
    },
}


def detect_lead_magnet(text: str) -> bool:
    low = (text or "").lower()
    return any(re.search(p, low) for p in _LEAD_MAGNET_CUES)


def _finalize(post_type, pave_surface, use_case, tools, reason, source) -> PostClassification:
    qualifies = post_type in QUALIFYING_POST_TYPES and pave_surface != PaveSurface.none
    return PostClassification(
        post_type=post_type, pave_surface=pave_surface, use_case=use_case,
        qualifies=qualifies, tools_mentioned=tools, reason=reason, source=source,
    )


def classify_post_live(post: dict) -> PostClassification:
    """Call the post-type model with a schema-constrained response."""
    content = post.get("post_text") or post.get("content") or post.get("title") or ""
    hint = ""
    if detect_lead_magnet(content):
        hint = (
            "\n\nNote: this post has a 'comment for an asset' call-to-action. If its asset "
            "maps to a Pave surface, it is lead_magnet; if off Pave's surface, off_topic."
        )

    from anthropic import Anthropic  # lazy: deterministic mode needs no SDK

    client = Anthropic()
    resp = client.messages.create(
        model=POST_CLASSIFIER_MODEL,
        max_tokens=350,
        system=_system_prompt(),
        messages=[{"role": "user", "content": content[:1200] + hint}],
        output_config={"format": {"type": "json_schema", "schema": _POST_SCHEMA}},
    )
    p = json.loads("".join(b.text for b in resp.content if getattr(b, "type", None) == "text"))
    return _finalize(
        PostType(p["post_type"]), PaveSurface(p["pave_surface"]),
        p.get("use_case", ""), p.get("tools_mentioned", []), p["reason"], "live",
    )
