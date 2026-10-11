"""Characterization test for the FRESH stress set (evals/heldout2.json).

This set was written BEFORE the four fixes it measures (leading "The", UPC leading-zero forms, singular/plural,
"dba" containment), so the fixes could not be tuned to it. Baseline before the fixes: the code's own queries
found 3 of 11 positive cases. After: 9 of 11. The two still missed were deliberately not fixed.
Like heldout.json it is a stress test, not a representative sample.
"""
from pathlib import Path

from evals.harness import load_cases, run_offline

FRESH = Path(__file__).parent.parent / "evals" / "heldout2.json"

KNOWN_GAPS = {
    "HX02": "UPC printed without its leading zero AND grouped 1-5-5: a form the fix does not generate",
    "HX06": "irregular plural 'bay leaf' vs 'bay leaves': neither the plan nor the matcher handles it",
}
KNOWN_OVERBLOCK = {"HX11": "'Sun Foods' vs 'Sunset Foods' scores as a close name, so a correct CLEAR is sent to review"}


def report():
    return run_offline(load_cases(FRESH))


def test_the_fresh_set_loads_and_has_distinct_ids():
    fresh = load_cases(FRESH)
    ids = {c.id for c in fresh}
    assert len(fresh) == 14 and not ids & {c.id for c in load_cases()}


def test_known_coverage_gaps_are_exactly_these():
    gaps = {v["case"] for v in report()["violations"] if v["invariant"] == "COVER-1"}
    assert gaps == set(KNOWN_GAPS), f"coverage changed. Now missing: {sorted(gaps)}"


def test_the_one_remaining_matcher_blind_spot_is_the_irregular_plural():
    by = {}
    for v in report()["violations"]:
        by.setdefault(v["invariant"], set()).add(v["case"])
    assert by.get("SAFE-1") == {"HX06"} and by.get("SAFE-4") == {"HX06"}


def test_a_precision_cost_is_known_and_in_the_safe_direction():
    """Over-blocking sends a correct CLEAR to a human. It never produces a false hold or a false clear."""
    by = {}
    for v in report()["violations"]:
        by.setdefault(v["invariant"], set()).add(v["case"])
    assert by.get("LIVE-1") == set(KNOWN_OVERBLOCK)


def test_no_false_hold_fabricated_evidence_or_crash_on_the_fresh_set():
    inv = report()["invariants"]
    for k in ("SAFE-2", "SAFE-3", "SAFE-5", "CRASH"):
        assert inv[k]["violations"] == 0, k


def test_the_precision_controls_stay_clear_when_nothing_resembles_them():
    from evals.harness import ScriptedClient, proposals, run_one
    for cid in ("HX10", "HX12"):
        case = next(c for c in load_cases(FRESH) if c.id == cid)
        r = run_one(case, ScriptedClient(case.searches, proposals(case)["clear"]))
        assert r["decision"] == "CLEAR" and r["matcher"]["verdict"] == "none", cid
