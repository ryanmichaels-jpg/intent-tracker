"""Deterministic unit tests for the intent miner's zero-cred layers.

These cover the trust-critical code that runs WITHOUT the LLM: the verbatim gate,
the praise verifier, richness scoring, comp-tool matching, and an end-to-end
deterministic miner run over the synthetic demo fixture. Runnable two ways:
    pytest tests/test_intent.py
    python3 tests/test_intent.py        (no pytest needed)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intent.comp_tools import matched_comp_tools
from intent.gate import decide, is_verbatim
from intent.richness import score_richness
from intent.schema import Classification, Commenter, Decision, IntentType, Lead
from intent.verify import looks_like_praise, verify


def test_verbatim_gate_rejects_hallucinated_quote():
    c = Commenter(name="x", comment_text="we need salary benchmarks before merit cycle")
    cls = Classification(intent_type=IntentType.active_need, need="n",
                         evidence_quote="needs benchmarks now", confidence=0.95, suggested_angle="a")
    decision, _ = decide(c, cls)
    assert decision == Decision.drop, "non-verbatim quote must drop even at high confidence"


def test_verbatim_gate_surfaces_real_quote():
    c = Commenter(name="x", comment_text="actively evaluating tools to replace our merit matrix")
    cls = Classification(intent_type=IntentType.evaluating, need="n",
                         evidence_quote="evaluating tools to replace our merit matrix",
                         confidence=0.8, suggested_angle="a")
    decision, _ = decide(c, cls)
    assert decision == Decision.surface


def test_low_confidence_routes_to_review():
    c = Commenter(name="x", comment_text="might look at benchmarking someday")
    cls = Classification(intent_type=IntentType.evaluating, need="n",
                         evidence_quote="might look at benchmarking someday",
                         confidence=0.4, suggested_angle="a")
    decision, _ = decide(c, cls)
    assert decision == Decision.review


def test_is_verbatim():
    assert is_verbatim("merit cycle", "running our merit cycle now")
    assert not is_verbatim("merit cycle", "annual review")
    assert not is_verbatim("", "anything")


def test_confidence_clamped():
    assert Classification(IntentType.curious, "n", "q", 1.7, "a").confidence == 1.0
    assert Classification(IntentType.curious, "n", "q", -0.2, "a").confidence == 0.0


def test_praise_verifier_downgrades():
    assert looks_like_praise("love this! great post")
    assert not looks_like_praise("love this benchmarking tool, switching from Radford")
    lead = Lead(
        commenter=Commenter(name="x", comment_text="love this! amazing"),
        decision=Decision.surface, reason="r",
        classification=Classification(IntentType.active_need, "n", "love this", 0.9, "a"),
    )
    new_decision, flag = verify(lead)
    assert new_decision == Decision.review and flag


def test_richness_levels():
    assert score_richness("BANDS")[1] == "thin"
    # names a tool + states a need + long -> rich
    s, label = score_richness(
        "We're ripping out our Radford spreadsheet and need benchmarking before merit cycle, what's everyone using?"
    )
    assert s == 3 and label == "rich"


def test_comp_tools_whole_word():
    assert "radford" in matched_comp_tools("we use Radford for surveys")
    assert matched_comp_tools("our company policy") == []  # 'compa' must not fire inside 'company'


def test_deterministic_miner_over_demo_fixture(tmp_path=None):
    """No key -> every comment becomes a 'pending' lead with a richness score; praise
    rows still appear (decision deferred), reaction-only rows are skipped."""
    from intent.run_miner import run

    os.environ.pop("ANTHROPIC_API_KEY", None)  # force deterministic mode
    dump = os.path.join(os.path.dirname(__file__), "..", "intent", "demo", "demo_comments_dump.json")
    out = os.path.join(os.path.dirname(__file__), "..", "data", "out", "leads-demo.csv")
    run(dump, week="demo", out_path=out)
    import csv
    rows = list(csv.DictReader(open(out)))
    assert len(rows) == 4, "4 comments across the 2 demo posts (all have text)"
    assert all(r["decision"] == "pending" for r in rows), "deterministic mode defers classification"
    assert {r["richness"] for r in rows} >= {"thin", "rich"}, "richness scored without the LLM"


def test_discover_stop_list_and_bait():
    from intent.discover import bait_score, excluded_reason, matched_displaced_tools
    assert excluded_reason("great staffing agency tips", "Recruiter") is not None
    assert excluded_reason("how do you benchmark comp?", "Head of Total Rewards") is None
    assert bait_score("comment below and I'll send you the bands template") >= 2
    assert "radford" in matched_displaced_tools("ditching Radford for benchmarking")
    assert matched_displaced_tools("our company values") == []  # whole-word, not 'compa'


def test_icp_parity_with_scrape():
    """intent/icp.py must behave byte-for-byte like scripts/scrape.py's ICP filter."""
    import importlib

    scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
    sys.path.insert(0, scripts_dir)
    scrape = importlib.import_module("scrape")
    from intent import icp

    battery = [
        "Head of Total Rewards at Acme", "VP People", "Chief People Officer",
        "Compensation Analyst", "Director of Compensation", "Senior Manager, Compensation",
        "Software Engineer", "Recruiter", "Student", "Account Executive", "Nurse",
        "Founder & CEO", "Data Scientist", "Total Rewards Lead", "", "Designer",
    ]
    for t in battery:
        assert icp.is_icp(t) == scrape.is_icp(t), f"is_icp parity broke on {t!r}"
        assert icp.is_non_icp(t) == scrape.is_non_icp(t), f"is_non_icp parity broke on {t!r}"


def test_logistics_downgraded():
    from intent.verify import looks_like_logistics, verify
    assert looks_like_logistics("London please.")
    assert looks_like_logistics("can you share details for Amsterdam, please?")
    assert not looks_like_logistics("what are you using for benchmarking?")     # comp signal
    assert not looks_like_logistics("we're evaluating tools to replace Radford") # comp signal + tool
    lead = Lead(
        commenter=Commenter(name="x", comment_text="London please."),
        decision=Decision.surface, reason="r",
        classification=Classification(IntentType.active_need, "n", "London please.", 0.8, "a"),
    )
    new_decision, flag = verify(lead)
    assert new_decision == Decision.review and "logistics" in flag


def test_competitor_employee_excluded():
    from intent.icp import is_competitor_employee
    assert is_competitor_employee("Bettercomp", "Co-Founder and CEO at Bettercomp")
    assert is_competitor_employee(None, "CEO at Figures")
    assert is_competitor_employee("Pave", "Account Executive")          # own company
    assert not is_competitor_employee("Northwind", "HR Director")       # real prospect
    assert not is_competitor_employee("Acme", "building spreadsheets")  # manual baseline != employer


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} deterministic tests passed.")


if __name__ == "__main__":
    _run_all()
