"""Orchestrator: turn a scraped comments dump into classified intent leads.

Input  : data/raw/comments_<date>.json  (written by scripts/scrape.py — the comment
         text + post body the company-posts actor already returned; gitignored).
Output : data/out/leads-<week>.csv      (gitignored; separate from append-<week>.csv).

Modes (auto-selected by ANTHROPIC_API_KEY):
  - live          : post-type gate conditions the classifier; intent classify ->
    verbatim gate -> verify (praise downgrade) -> richness -> decision.
  - deterministic : emit each ICP-passed commenter as a 'pending' lead with a richness
    score, awaiting classification. Re-run with the key set to light them up.

The existing scrape + Master append are untouched; this is purely additive.

Run:  python3 -m intent.run_miner --dump data/raw/comments_2026-06-29.json --week 2026-W27
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
from collections import Counter

from .gate import decide
from .richness import score_richness
from .schema import Commenter, Decision, Lead
from .verify import verify

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _live() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _commenter(c: dict, post: dict) -> Commenter:
    return Commenter(
        name=c.get("name") or "unknown",
        comment_text=(c.get("comment_text") or "").strip(),
        headline=c.get("headline"),
        company=c.get("company"),
        profile_url=c.get("profile_url"),
        competitor=post.get("competitor"),
        post_url=post.get("post_url"),
        source=c.get("source", "curated"),
    )


def process_comment(commenter: Commenter, pc, live: bool) -> Lead:
    richness, richness_label = score_richness(commenter.comment_text)
    post_type = pc.post_type if pc else None
    pave_surface = pc.pave_surface if pc else None

    if not live:
        return Lead(
            commenter=commenter, decision=Decision.pending,
            reason="awaiting classification (ANTHROPIC_API_KEY not set)",
            post_type=post_type, pave_surface=pave_surface,
            richness=richness, richness_label=richness_label,
        )

    from .classify import classify as classify_intent

    cls = classify_intent(commenter, post_type.value if post_type else None)
    decision, reason = decide(commenter, cls)
    lead = Lead(
        commenter=commenter, decision=decision, reason=reason,
        post_type=post_type, pave_surface=pave_surface, classification=cls,
        richness=richness, richness_label=richness_label,
    )
    new_decision, flag = verify(lead)
    if new_decision != lead.decision:
        lead.decision, lead.reason, lead.quality_flag = new_decision, flag, flag
    # Curated competitor sources are trusted: a non-qualifying post downgrades a
    # surface to review (so nothing is silently dropped), never a hard drop.
    if pc is not None and not pc.qualifies and lead.decision == Decision.surface:
        lead.decision = Decision.review
        lead.quality_flag = ((lead.quality_flag or "") + " | post did not qualify -- review").strip(" |")
        lead.reason = f"post not qualifying ({pc.post_type.value}/{pc.pave_surface.value}); {lead.reason}"
    return lead


def process_post(post: dict, live: bool) -> list[Lead]:
    pc = None
    if live:
        from .posttype import classify_post_live
        try:
            pc = classify_post_live(post)
        except Exception as e:  # post-type gate is advisory; never let it sink the run
            print(f"[miner] post-type gate failed for {post.get('post_url')}: {e}")
    leads = []
    for c in post.get("comments", []):
        commenter = _commenter(c, post)
        if not commenter.comment_text:
            continue  # reaction-only / empty -> nothing to classify
        leads.append(process_comment(commenter, pc, live))
    return leads


_FIELDS = [
    "decision", "richness", "intent", "confidence", "source", "name", "headline", "company",
    "competitor", "post_type", "pave_surface", "evidence_quote", "suggested_angle",
    "reason", "comment", "profile_url", "post_url",
]
_ORDER = {"surface": 0, "review": 1, "pending": 2, "drop": 3}
_RICH = {"rich": 0, "moderate": 1, "thin": 2}


def _record(lead: Lead) -> dict:
    c, cls = lead.commenter, lead.classification
    return {
        "decision": lead.decision.value,
        "richness": lead.richness_label or "",
        "intent": cls.intent_type.value if cls else "",
        "confidence": cls.confidence if cls else "",
        "source": c.source,
        "name": c.name,
        "headline": (c.headline or "").replace("\n", " "),
        "company": c.company or "",
        "competitor": c.competitor or "",
        "post_type": lead.post_type.value if lead.post_type else "",
        "pave_surface": lead.pave_surface.value if lead.pave_surface else "",
        "evidence_quote": cls.evidence_quote if cls else "",
        "suggested_angle": cls.suggested_angle if cls else "",
        "reason": lead.reason,
        "comment": " ".join(c.comment_text.split()),
        "profile_url": c.profile_url or "",
        "post_url": c.post_url or "",
    }


def write_leads(leads: list[Lead], out_path: str):
    records = sorted(
        (_record(x) for x in leads),
        key=lambda r: (_ORDER.get(r["decision"], 9), _RICH.get(r["richness"], 3)),
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_FIELDS)
        w.writeheader()
        w.writerows(records)
    return out_path, records


def latest_dump() -> str | None:
    cands = sorted(glob.glob(os.path.join(REPO, "data", "raw", "comments_*.json")))
    return cands[-1] if cands else None


def run(dump_path: str, week: str | None = None, out_path: str | None = None) -> str:
    with open(dump_path, "r", encoding="utf-8") as fh:
        dump = json.load(fh)
    posts = dump.get("posts", [])
    week = week or dump.get("week") or "unknown"
    live = _live()

    leads: list[Lead] = []
    for post in posts:
        leads.extend(process_post(post, live))

    out_path = out_path or os.path.join(REPO, "data", "out", f"leads-{week}.csv")
    out_path, records = write_leads(leads, out_path)
    counts = Counter(r["decision"] for r in records)
    print(f"[miner] mode={'live' if live else 'deterministic'} posts={len(posts)} "
          f"comments={len(records)} decisions={dict(counts)}")
    print(f"[miner] wrote {out_path}")
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", help="comments dump JSON (default: latest data/raw/comments_*.json)")
    ap.add_argument("--week", default=None, help="ISO week label for the output filename")
    ap.add_argument("--out", default=None, help="explicit output CSV path")
    a = ap.parse_args()
    dump = a.dump or latest_dump()
    if not dump:
        raise SystemExit("no comments dump found (data/raw/comments_*.json) — run the scrape first.")
    run(dump, a.week, a.out)


if __name__ == "__main__":
    main()
