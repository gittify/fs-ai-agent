"""Characterization test for the first stress set (evals/heldout.json).

Written AFTER the search plan, as deliberate probes of its limits. It documents what the code finds on
its own today. KNOWN_GAPS are KNOWN LIMITATIONS, not passing behavior: if you fix one, this test fails on
purpose, so you update KNOWN_GAPS and the docs together.

History: the first measurement found 7 gaps (HO04, HO06 to HO11) and a matcher blind spot (HO09, HO10).
HO04 (UPC leading zero), HO09 (dba name), HO10 (plural) and HO11 (leading "The") were then fixed. The set
is therefore no longer held out for those cases; evals/heldout2.json is the fresh set that measured the fixes.
"""
from pathlib import Path

from evals.harness import load_cases, run_offline

HELDOUT = Path(__file__).parent.parent / "evals" / "heldout.json"

KNOWN_GAPS = {
    "HO06": "'Pacific' is on the generic first-word list, so no first-word query; 'Intl' vs 'International'",
    "HO07": "'Sun-Rise' splits into two words, so the first-word query 'Sunrise' finds nothing",
    "HO08": "typo in the recall's firm name; phrase search cannot find it",
}


def test_the_stress_set_loads_and_does_not_collide_with_the_other_sets():
    stress, main = load_cases(HELDOUT), load_cases()
    assert len(stress) == 14 and not {c.id for c in stress} & {c.id for c in main}


def test_known_coverage_gaps_are_exactly_these():
    gaps = {v["case"] for v in run_offline(load_cases(HELDOUT))["violations"] if v["invariant"] == "COVER-1"}
    assert gaps == set(KNOWN_GAPS), f"coverage changed. Now missing: {sorted(gaps)}"


def test_the_matcher_blind_spot_found_on_this_set_is_closed():
    """HO09 (dba) and HO10 (plural) used to be retrieved but hidden by the matcher, ending in a false CLEAR."""
    report = run_offline(load_cases(HELDOUT))
    assert report["invariants"]["SAFE-1"]["violations"] == 0 and report["invariants"]["SAFE-4"]["violations"] == 0


def test_everything_else_holds_on_the_stress_set():
    inv = run_offline(load_cases(HELDOUT))["invariants"]
    for k in ("SAFE-2", "SAFE-3", "SAFE-5", "LIVE-1", "CRASH"):
        assert inv[k]["violations"] == 0, k
