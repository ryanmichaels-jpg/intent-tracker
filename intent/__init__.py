"""Pave intent miner — the trust layer that turns scraped comments into classified
intent leads. Bolts onto the existing weekly scrape (see HANDOFF.md); does not alter
the engagement/jobs tracks or the Master append. Deterministic stages run with zero
credentials; the LLM stages (posttype, classify) light up when ANTHROPIC_API_KEY is set.
"""
