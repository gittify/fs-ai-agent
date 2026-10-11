"""Offline tests: scripted fake Bedrock client + openFDA fault injection."""
import pytest

from recall_agent import openfda
from recall_agent.agent import run_item
from recall_agent.matcher import Item, firm_score


def _resp(*blocks):
    return {"output": {"message": {"role": "assistant", "content": list(blocks)}}, "stopReason": "tool_use"}


def search(term="Sunrise Foods Inc", i="s1"):
    return {"toolUse": {"toolUseId": i, "name": "search_fda_recalls",
                        "input": {"field": "recalling_firm", "term": term}}}


def decide(decision, conf=0.9, recalls=(), i="d1"):
    return {"toolUse": {"toolUseId": i, "name": "record_decision",
                        "input": {"decision": decision, "confidence": conf,
                                  "recall_numbers": list(recalls), "reasoning": "test"}}}


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)

    def converse(self, **kw):
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setenv("FDA_BACKOFF_BASE", "0")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "test-model")


def run(item, fault, *model_turns, monkeypatch):
    monkeypatch.setenv("FDA_FAULT", fault)
    return run_item(FakeClient(*model_turns), item)


SUPPLIER = Item("Sunrise Foods Inc", "supplier", lots=["LOT 7710B"])


def test_404_not_found_is_clear_not_error(monkeypatch):
    r = run(SUPPLIER, "not_found", _resp(search()), _resp(decide("CLEAR")), monkeypatch=monkeypatch)
    assert r["decision"] == "CLEAR"
    assert r["tool_calls"][0]["status"] == "clear"


def test_rate_limit_forces_not_checked_even_if_model_says_clear(monkeypatch):
    r = run(SUPPLIER, "rate_limit", _resp(search()), _resp(decide("CLEAR")), monkeypatch=monkeypatch)
    assert r["decision"] == "NOT_CHECKED"
    assert r["tool_calls"][0]["attempts"] == 4


@pytest.mark.parametrize("fault", ["garbage", "malformed", "timeout"])
def test_garbage_or_timeout_never_becomes_clear(monkeypatch, fault):
    r = run(SUPPLIER, fault, _resp(search()), _resp(decide("CLEAR")), monkeypatch=monkeypatch)
    assert r["decision"] == "NOT_CHECKED"


def test_lookalike_firm_with_model_hold_goes_to_review(monkeypatch):
    r = run(SUPPLIER, "lookalike", _resp(search()),
            _resp(decide("HOLD", 0.9, ["F-1234-2026"])), monkeypatch=monkeypatch)
    assert r["matcher"]["verdict"] == "possible"
    assert r["decision"] == "NEEDS_REVIEW"
    assert "matcher" in r["override_reason"]


def test_lookalike_firm_with_model_clear_also_goes_to_review(monkeypatch):
    r = run(SUPPLIER, "lookalike", _resp(search()), _resp(decide("CLEAR")), monkeypatch=monkeypatch)
    assert r["decision"] == "NEEDS_REVIEW"


def test_confirmed_firm_and_lot_match_holds(monkeypatch):
    item = Item("Sunrise Food Co", "supplier", lots=["LOT 8841A"])
    r = run(item, "lookalike", _resp(search("Sunrise Food Co")),
            _resp(decide("HOLD", 0.92, ["F-1234-2026"])), monkeypatch=monkeypatch)
    assert r["matcher"]["verdict"] == "strong"
    assert r["decision"] == "HOLD"


def test_hallucinated_recall_number_is_caught(monkeypatch):
    item = Item("Sunrise Food Co", "supplier", lots=["LOT 8841A"])
    r = run(item, "lookalike", _resp(search("Sunrise Food Co")),
            _resp(decide("HOLD", 0.95, ["F-9999-2026"])), monkeypatch=monkeypatch)
    assert r["decision"] == "NEEDS_REVIEW"


def test_model_call_failure_is_not_checked(monkeypatch):
    class Boom:
        def converse(self, **kw):
            raise RuntimeError("ThrottlingException")
    monkeypatch.setenv("FDA_FAULT", "not_found")
    r = run_item(Boom(), SUPPLIER)
    assert r["decision"] == "NOT_CHECKED"
    assert "ThrottlingException" in r["model_error"]


def test_firm_score_levels():
    assert firm_score("Sunrise Foods Inc", "SUNRISE FOODS, LLC") == 1.0
    assert 0.75 <= firm_score("Sunrise Foods Inc", "Sunrise Food Company") < 1.0
    assert firm_score("Sunrise Foods Inc", "Acme Spice Traders") < 0.5


def test_query_builder_keeps_openfda_syntax():
    q = openfda.build_query("recalling_firm", 'Sun "rise" Foods', 30)
    assert q.startswith('recalling_firm:"Sun+rise+Foods"+AND+report_date:[')
