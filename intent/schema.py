"""Pave intent-miner trust contract (stdlib only — no pydantic dependency).

Mirrors the proven Figma engine's schema, adapted for Pave (comp / total rewards)
and stripped of the pieces this deployment doesn't use (no Salesforce Account /
Routing, no Slack/pipeline tracking). Dataclasses + enums keep the deterministic
pipeline runnable with zero installs; the LLM classifier is still schema-constrained
at the API (see classify.py) and its 0-1 confidence is validated here after parsing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class IntentType(str, Enum):
    active_need = "active_need"   # running a comp cycle / needs benchmarks now / fighting spreadsheets
    evaluating = "evaluating"     # comparing comp tools or survey providers
    curious = "curious"           # passive interest, no project
    noise = "noise"               # praise / off-topic / not about doing comp work


class Decision(str, Enum):
    surface = "surface"
    review = "review"
    drop = "drop"
    pending = "pending"   # deterministic run before ANTHROPIC_API_KEY is set: awaiting classification


class PostType(str, Enum):
    """What kind of post this is — decided BEFORE judging its comments.

    Only the first three qualify as clean lead sources; showcases draw praise and
    off_topic posts aren't about comp tooling. For CURATED competitor sources we
    don't hard-drop on this (we trust the source); it conditions the classifier and
    a non-qualifying post downgrades a surface to review rather than dropping it.
    """

    lead_magnet = "lead_magnet"          # "comment 'bands' and I'll send the template"
    tool_question = "tool_question"      # "what are you using for benchmarking?"
    tool_comparison = "tool_comparison"  # "Radford vs Mercer?"
    showcase = "showcase"                # shows a build/result, mostly praise
    off_topic = "off_topic"              # not about comp tooling at all


QUALIFYING_POST_TYPES = {PostType.lead_magnet, PostType.tool_question, PostType.tool_comparison}


class PaveSurface(str, Enum):
    """Which Pave surface could displace the post's use case."""

    benchmarking = "benchmarking"
    comp_planning = "comp_planning"
    pay_bands = "pay_bands"
    pay_equity = "pay_equity"
    total_rewards = "total_rewards"
    offers = "offers"
    none = "none"  # Pave cannot displace this use case


@dataclass
class Commenter:
    """A person who commented on a competitor/comp post (already ICP-filtered upstream
    by the scrape; we do not re-apply the ICP filter here)."""

    name: str
    comment_text: str
    headline: Optional[str] = None
    company: Optional[str] = None
    profile_url: Optional[str] = None
    competitor: Optional[str] = None   # which competitor's post they engaged with
    post_url: Optional[str] = None
    source: str = "live"


@dataclass
class PostClassification:
    post_type: PostType
    pave_surface: PaveSurface = PaveSurface.none
    use_case: str = ""
    qualifies: bool = False
    tools_mentioned: list = field(default_factory=list)
    reason: str = ""
    source: str = "demo"


@dataclass
class Classification:
    """Schema-constrained LLM output. The API can't enforce numeric min/max, so the
    0-1 confidence is clamped/validated here after parsing."""

    intent_type: IntentType
    need: str
    evidence_quote: str
    confidence: float
    suggested_angle: str

    def __post_init__(self):
        if not isinstance(self.confidence, (int, float)):
            raise ValueError("confidence must be numeric")
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        if not isinstance(self.intent_type, IntentType):
            self.intent_type = IntentType(self.intent_type)


@dataclass
class Lead:
    """A commenter after the pipeline: (post-type) -> classify -> gate -> verify -> richness."""

    commenter: Commenter
    decision: Decision
    reason: str
    post_type: Optional[PostType] = None
    pave_surface: Optional[PaveSurface] = None
    classification: Optional[Classification] = None
    quality_flag: Optional[str] = None
    richness: Optional[int] = None
    richness_label: Optional[str] = None


def classification_json_schema() -> dict:
    """Flat JSON Schema for the Anthropic structured-output config. additionalProperties
    false; no minimum/maximum on the number field (the API rejects them — validated in
    Classification.__post_init__ instead)."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["intent_type", "need", "evidence_quote", "confidence", "suggested_angle"],
        "properties": {
            "intent_type": {
                "type": "string",
                "enum": [e.value for e in IntentType],
                "description": (
                    "active_need: running a comp cycle / needs benchmarks now / fighting "
                    "spreadsheets; evaluating: comparing comp tools or survey providers; "
                    "curious: passive interest, no project; noise: praise/off-topic/not about "
                    "doing comp work."
                ),
            },
            "need": {"type": "string", "description": "One sentence summarizing the stated comp need."},
            "evidence_quote": {
                "type": "string",
                "description": "A verbatim substring copied exactly from the comment. Do not paraphrase or invent.",
            },
            "confidence": {"type": "number", "description": "Confidence from 0.0 to 1.0."},
            "suggested_angle": {
                "type": "string",
                "description": "One-line rep talking point: how Pave fits the stated comp need.",
            },
        },
    }
