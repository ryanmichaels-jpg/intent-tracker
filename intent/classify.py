"""Intent classifier — the one bounded LLM step.

Live mode calls Anthropic with structured output so the response is
schema-constrained: the model must return every field, and the gate (gate.py) still
rejects any non-verbatim evidence quote. The cheap haiku model runs only on
commenters that already passed the scrape-side ICP filter. Borderline calls are
re-sampled and majority-voted (self-consistency). Without ANTHROPIC_API_KEY the
caller skips classification (deterministic run).
"""
from __future__ import annotations

import json
import os
from collections import Counter

from .schema import Classification, Commenter, IntentType, classification_json_schema

SELF_CONSISTENCY_SAMPLES = int(os.environ.get("SELF_CONSISTENCY_SAMPLES", "3"))
_SURFACE_INTENTS = {IntentType.active_need, IntentType.evaluating}
# tie-break order: most conservative first
_INTENT_PRIORITY = [IntentType.noise, IntentType.curious, IntentType.evaluating, IntentType.active_need]

CLASSIFIER_MODEL = os.environ.get("CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")

SYSTEM_PROMPT = (
    "You classify LinkedIn comments to find people in the market for a compensation / "
    "total-rewards tool like Pave. The comments are on posts by comp tools and comp "
    "creators (benchmarking, pay bands, merit cycles, pay equity, total rewards). A "
    "strong lead is a comp/people/finance leader actively running a comp cycle, needing "
    "market benchmarks, fighting spreadsheets, or comparing comp tools/survey providers. "
    "Return only the requested JSON. The evidence_quote MUST be copied verbatim from the "
    "comment — an exact substring, no paraphrasing, no invention. If the comment is "
    "praise, off-topic, or not about doing comp work, use intent_type 'noise'. Ground "
    "need and suggested_angle only in what the comment says; suggested_angle is a one-line "
    "angle for how Pave (benchmarking, comp planning, pay bands, pay equity, total "
    "rewards) fits the stated need."
)

# How the post type changes the reading of a comment (the "post is the prior" rule).
_POST_CONTEXT = {
    "lead_magnet": (
        "This is a lead-magnet post (the author offers a guide/template for commenting), "
        "so a short comment asking for the asset ('bands', 'interested', 'send it') IS an "
        "active hand-raise — treat it as active_need, not noise."
    ),
    "tool_question": "This post asks what comp tools people use, so naming a tool or a need is evaluating/active_need.",
    "tool_comparison": "This post compares comp tools, so stating a preference or gap is evaluating.",
}


def classify_live(commenter: Commenter, post_type: str | None = None) -> Classification:
    """Call the classifier with a schema-constrained response, conditioned on post type."""
    from anthropic import Anthropic  # lazy so deterministic mode needs no SDK

    client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    context = _POST_CONTEXT.get(post_type or "", "")
    user = (
        f"Post type: {post_type or 'unknown'}. {context}\n"
        f"Comment: {commenter.comment_text!r}\n"
        f"Author headline: {commenter.headline or 'unknown'}\n"
        "Classify this commenter's compensation-tool intent."
    )
    resp = client.messages.create(
        model=CLASSIFIER_MODEL,
        max_tokens=400,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": classification_json_schema()}},
    )
    payload = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return Classification(**json.loads(payload))


def _is_borderline(cls: Classification) -> bool:
    """A surface-eligible call that is easy to flip -> worth re-sampling."""
    return cls.intent_type in _SURFACE_INTENTS and (
        cls.confidence < 0.8 or len(cls.evidence_quote.split()) <= 2
    )


def resolve_votes(votes: list[Classification]) -> Classification:
    """Majority-vote intent; conservative tie-break; scale confidence by agreement so a
    split vote drops below the surface threshold and routes to review."""
    counts = Counter(v.intent_type for v in votes)
    top = max(counts.values())
    winners = [i for i, c in counts.items() if c == top]
    winner = min(winners, key=lambda i: _INTENT_PRIORITY.index(i))
    agreement = counts[winner] / len(votes)
    rep = next(v for v in votes if v.intent_type == winner)
    return Classification(
        intent_type=winner,
        need=rep.need,
        evidence_quote=rep.evidence_quote,
        confidence=round(rep.confidence * agreement, 2),
        suggested_angle=rep.suggested_angle,
    )


def classify(commenter: Commenter, post_type: str | None = None) -> Classification:
    """Live classification with self-consistency on borderline calls."""
    first = classify_live(commenter, post_type)
    if not _is_borderline(first):
        return first
    votes = [first] + [
        classify_live(commenter, post_type) for _ in range(max(0, SELF_CONSISTENCY_SAMPLES - 1))
    ]
    return resolve_votes(votes)
