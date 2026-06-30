"""The trust gate (Pave).

No lead is surfaced without a verbatim evidence quote drawn from the real comment.
A deterministic check the LLM cannot talk past: if the quote is not an exact
substring of the comment, the lead is dropped even when the model was confident.

Adapted from the Figma engine, persona-free: this deployment keeps the existing
scrape-side ICP filter (commenters arriving here already passed it), so there is no
buyer/builder persona branching — the surface decision rests on verbatim evidence +
intent + confidence.
"""
from __future__ import annotations

from .schema import Classification, Commenter, Decision, IntentType

CONFIDENCE_THRESHOLD = 0.6
SURFACE_INTENTS = {IntentType.active_need, IntentType.evaluating}


def is_verbatim(quote: str, comment: str) -> bool:
    """True only if `quote` is a non-empty exact substring of `comment`."""
    q = (quote or "").strip()
    return bool(q) and q in (comment or "")


def decide(commenter: Commenter, cls: Classification) -> tuple[Decision, str]:
    """Apply the gate to a classified commenter. Verbatim check is first so a
    hallucinated quote can never be rescued by a high confidence score."""
    if not is_verbatim(cls.evidence_quote, commenter.comment_text):
        return Decision.drop, "evidence quote not verbatim -- failed trust gate"

    if cls.intent_type == IntentType.noise:
        return Decision.drop, "intent classified as noise"

    if cls.intent_type in SURFACE_INTENTS and cls.confidence >= CONFIDENCE_THRESHOLD:
        return (
            Decision.surface,
            f"{cls.intent_type.value} intent at confidence {cls.confidence:.2f}",
        )

    return (
        Decision.review,
        f"ambiguous: {cls.intent_type.value} intent at confidence {cls.confidence:.2f}",
    )
