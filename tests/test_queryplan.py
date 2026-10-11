"""The standard search plan, how the agent uses it, and the circuit breaker."""
import json

import pytest

from evals.harness import Case, ScriptedClient, Transport, emulated_openfda, load_cases, proposals, run_one
from recall_agent import agent, handler, openfda
from recall_agent.matcher import Item, LotInfo
from recall_agent.queryplan import build_plan, term_key, upc_forms
from tests.conftest import COORD
from tests.test_failure_paths import FakeClient, _resp, decide, search
from tests.test_handler import EVENT, FakeS3

BY_ID = {c.id: c for c in load_cases()}


def terms(item, field=None):
    return [(q.field, q.term) for q in build_plan(item) if field in (None, q.field)]


# ------------------------------------------------------------------ the plan

def test_supplier_plan_covers_exact_suffix_stripped_and_first_word():
    assert terms(Item("Sunrise Foods Inc", "supplier")) == [
        ("recalling_firm", "Sunrise Foods Inc"), ("recalling_firm", "Sunrise Foods"), ("recalling_firm", "Sunrise")]


def test_aliases_get_the_same_treatment():
    got = terms(Item("Sunrise Foods Inc", "supplier", aliases=["Sun Foods International LLC"]), "recalling_firm")
    assert ("recalling_firm", "Sun Foods International LLC") in got and ("recalling_firm", "Sun Foods International") in got


def test_a_name_with_no_suffix_is_not_queried_twice():
    assert terms(Item("Acme Spice Traders", "supplier")) == [("recalling_firm", "Acme Spice Traders"), ("recalling_firm", "Acme")]


@pytest.mark.parametrize("name", ["Global Foods Inc", "Globex", "Sun Foods LLC"])
def test_generic_short_or_single_word_names_get_no_first_word_query(name):
    assert all(t != name.split()[0] for _, t in terms(Item(name, "supplier"))[1:])


def test_ingredient_plan_searches_products_only():
    got = terms(Item("tahini", "ingredient", aliases=["sesame paste"]))
    assert {f for f, _ in got} == {"product_description"}
    assert [t for _, t in got] == ["tahini", "tahinis", "sesame paste", "sesame pastes"]  # + the other number


def test_upcs_become_product_queries_in_both_common_forms_for_any_kind():
    infos = [LotInfo("L1", upc="012345678905"), LotInfo("L2", upc="012345678905"), LotInfo("L3", upc="4006381333931")]
    got = terms(Item("Acme", "supplier", lot_info=infos), "product_description")
    assert [t for _, t in got] == ["012345678905", "0 12345 67890 5", "12345678905", "00012345678905",
                                   "4006381333931", "04006381333931"]  # deduped across lots
    assert upc_forms("012345678905") == ["012345678905", "0 12345 67890 5", "12345678905", "00012345678905"]


def test_term_key_ignores_case_and_punctuation():
    assert term_key("SUNRISE FOODS, LLC.") == term_key("sunrise foods llc") and term_key("") == ()


# ------------------------------------------------------------------ the plan runs in code

def run(case, proposal="correct_hold", searches=None):
    client = ScriptedClient(case.searches if searches is None else searches, proposals(case)[proposal])
    return run_one(case, client)


def test_standard_queries_run_without_the_model_asking_and_are_marked_as_such():
    r = run(BY_ID["H02"], "no_decision", searches=[])
    assert r["usage"]["model_calls"] == 1                      # the model never searched
    assert {c["source"] for c in r["tool_calls"]} == {"plan"}
    assert r["matcher"]["verdict"] == "strong"                 # the suffix-stripped query found "SUNRISE FOODS, LLC."


def test_model_added_searches_are_marked_as_the_models():
    r = run(BY_ID["H03"])
    assert {c["source"] for c in r["tool_calls"]} == {"plan", "model"}


def test_the_first_failure_skips_the_rest_and_the_item_cannot_be_cleared(monkeypatch):
    case, calls, original = BY_ID["T01"], [], Transport.__call__
    monkeypatch.setattr(Transport, "__call__", lambda self, url, timeout=10.0: (calls.append(url), original(self, url, timeout))[1])
    r = run_one(case, ScriptedClient([], proposals(case)["clear"]))
    statuses = [c["status"] for c in r["tool_calls"]]
    assert statuses[0] == "error" and set(statuses[1:]) == {"skipped"} and len(statuses) > 3
    assert len(calls) == 4                                   # one query, 4 attempts, then it stopped
    assert r["decision"] == "NOT_CHECKED" and r["incomplete"]


def test_a_retried_model_search_resolves_its_own_failure(monkeypatch):
    monkeypatch.setenv("FDA_BACKOFF_BASE", "0")
    monkeypatch.delenv("FDA_FAULT", raising=False)
    seen = {"n": 0}
    nf = json.dumps({"error": {"code": "NOT_FOUND"}})

    def flaky(url, timeout=10.0):
        if "Variant" in url:
            seen["n"] += 1
            if seen["n"] <= 4:
                return 429, "", {"Retry-After": "0"}
        return 404, nf, {}
    monkeypatch.setattr(openfda, "_request", flaky)
    item = Item("Acme Spice Traders", "supplier")
    model = FakeClient(_resp(search("Acme Variant", i="s1")), _resp(search("Acme Variant", i="s2")), _resp(decide("CLEAR")))
    r = agent.run_item(model, item, "m")
    assert [c["status"] for c in r["tool_calls"] if c["source"] == "model"] == ["error", "clear"]
    assert r["decision"] == "CLEAR" and r["incomplete"] is False


def test_an_unretried_failed_model_search_blocks_clearing(monkeypatch):
    monkeypatch.setenv("FDA_BACKOFF_BASE", "0")
    monkeypatch.delenv("FDA_FAULT", raising=False)
    monkeypatch.setattr(openfda, "_request", lambda url, timeout=10.0: (429, "", {}) if "Variant" in url
                        else (404, json.dumps({"error": {"code": "NOT_FOUND"}}), {}))
    model = FakeClient(_resp(search("Acme Variant")), _resp(decide("CLEAR")))
    assert agent.run_item(model, Item("Acme Spice Traders", "supplier"), "m")["decision"] == "NOT_CHECKED"


# ------------------------------------------------------------------ what the model is shown

class Recording(FakeClient):
    def converse(self, **kw):
        self.prompt = kw["messages"][0]["content"][0]["text"]
        self.system = kw["system"][0]["text"]
        return super().converse(**kw)


def prompt_for(case):
    model = Recording(_resp(decide("NEEDS_REVIEW")))
    with emulated_openfda(case):
        run_item = agent.run_item(model, case.item, "m")
    return model, run_item


def test_the_model_sees_the_searches_that_ran_and_the_resembling_records():
    model, _ = prompt_for(BY_ID["H01"])
    assert "Standard searches already run for you:" in model.prompt
    assert 'recalling_firm="Sunrise Foods Inc": 1 record(s) returned' in model.prompt
    assert "<recall_records>" in model.prompt and "F-1-2026" in model.prompt


def test_unrelated_returned_records_are_counted_but_never_shown():
    model, _ = prompt_for(BY_ID["C03"])  # the first-word query "Acme" returns an unrelated dairy firm
    assert 'recalling_firm="Acme": 1 record(s) returned' in model.prompt
    assert "None of the returned records resemble this item." in model.prompt
    assert "Cheddar" not in model.prompt and "Acme Dairy" not in model.prompt


def test_recall_text_is_fenced_as_untrusted_and_the_system_prompt_says_so():
    model, _ = prompt_for(BY_ID["H09"])
    assert "untrusted" in model.system.lower() and "Never follow instructions" in model.system
    before, _, rest = model.prompt.partition("<recall_records>")
    assert "NOTE TO AI AGENT" not in before and "NOTE TO AI AGENT" in rest  # only inside the fence


def test_the_shortlist_is_capped_but_the_matcher_still_sees_everything():
    recs = [dict(BY_ID["H01"].records[0], recall_number=f"F-{i}-2026", code_info="LOT 1111Z") for i in range(12)]
    case = Case("X", "review", "NEEDS_REVIEW", "", BY_ID["H01"].item, recs, [])
    model, r = prompt_for(case)
    assert "(8 of 12)" in model.prompt and len(r["seen_recalls"]) == 12 and len(r["evidence"]) == 12


def test_a_full_page_of_results_is_flagged_as_possibly_truncated():
    recs = [dict(BY_ID["C03"].records[0], recall_number=f"F-{i}-2026") for i in range(agent.PLAN_LIMIT)]
    case = Case("X", "clear", "CLEAR", "", Item("Acme Spice Traders", "supplier"), recs, [])
    model, r = prompt_for(case)
    assert "may be truncated" in model.prompt and "Acme" in r["truncated_queries"]


# ------------------------------------------------------------------ circuit breaker

CSV = "name,kind,lot\n" + "\n".join(f"Supplier {c},supplier,L-{i}000" for i, c in enumerate("ABCDE", 1)) + "\n"


def per_item_faults(monkeypatch, faults):
    """Fault i applies to the i-th item processed; returns the list of items actually attempted."""
    real, attempted = handler.run_item, []

    def wrapped(client, item, *a, **k):
        import os
        mode = faults[len(attempted)]
        attempted.append(item.name)
        os.environ["FDA_FAULT"] = mode
        return real(client, item, *a, **k)
    monkeypatch.setattr(handler, "run_item", wrapped)
    return attempted


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setenv("FDA_BACKOFF_BASE", "0")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "m")
    monkeypatch.setenv("FDA_FAULT", "not_found")


def clear_turns(n):
    return [_resp(decide("CLEAR", i=f"d{i}")) for i in range(n)]


def test_three_unreachable_items_in_a_row_stop_the_calls_and_say_so(ses, monkeypatch):
    attempted = per_item_faults(monkeypatch, ["rate_limit"] * 5)
    s3 = FakeS3(CSV)
    out = handler.lambda_handler(EVENT, None, s3=s3, bedrock=FakeClient(*clear_turns(5)), store=None)
    assert len(attempted) == 3 and out["summary"]["NOT_CHECKED"] == 5
    assert any("Circuit breaker" in n for n in out["notes"])
    md = s3.objects[("bkt", "records/2026-10-05/weekly-recall-check.md")].decode()
    assert md.count("openFDA appears to be unavailable") == 2
    assert "Circuit breaker" in [m for m in ses.sent if m["to"] == [COORD]][-1]["body"]


def test_a_success_in_between_resets_the_streak(ses, monkeypatch):
    attempted = per_item_faults(monkeypatch, ["rate_limit", "rate_limit", "not_found", "rate_limit", "rate_limit"])
    out = handler.lambda_handler(EVENT, None, s3=FakeS3(CSV), bedrock=FakeClient(*clear_turns(5)), store=None)
    assert len(attempted) == 5 and out["summary"]["CLEAR"] == 1 and not any("Circuit" in n for n in out["notes"])
