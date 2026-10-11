"""Run metrics (CloudWatch EMF), and the injection measurement set."""
import json

import pytest

from evals.harness import ScriptedClient, load_cases, proposals, run_offline, run_one
from recall_agent import handler, metrics
from recall_agent.guard import Guardrail
from tests.conftest import COORD
from tests.test_failure_paths import FakeClient, _resp, decide
from tests.test_guardrails import fake_guard
from tests.test_handler import EVENT, FakeS3
from tests.test_notify import FakeStore

CSV = "name,kind,lot\nAcme Spice Traders,supplier,\nInitech Foods,supplier,\n"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("FDA_BACKOFF_BASE", "0")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "m")
    monkeypatch.setenv("FDA_FAULT", "not_found")
    monkeypatch.delenv("GUARDRAIL_ID", raising=False)


def emf_lines(capsys):
    return [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith("{") and "_aws" in l]


def run(csv=CSV, store=None, bedrock=None, s3=None):
    return handler.lambda_handler(EVENT, None, s3=s3 or FakeS3(csv), store=store,
                                  bedrock=bedrock or FakeClient(*[_resp(decide("CLEAR", i=f"d{i}")) for i in range(9)]))


# ------------------------------------------------------------------ the EMF contract

def test_every_metric_has_a_unit_and_the_names_are_unique():
    assert len(set(metrics.METRIC_NAMES)) == len(metrics.METRIC_NAMES) and set(metrics.UNITS.values()) <= {"Count", "Seconds"}


def test_the_line_is_valid_embedded_metric_format():
    line = json.loads(metrics.emit({"ItemsHold": 1, "RunDurationSeconds": 2.5}, stream=open("/dev/null", "w"), now=1_700_000_000))
    meta = line["_aws"]
    assert meta["Timestamp"] == 1_700_000_000_000 and isinstance(meta["Timestamp"], int)
    [directive] = meta["CloudWatchMetrics"]
    assert directive["Namespace"] == "RecallAgent" and directive["Dimensions"] == [["Service"]]
    assert {m["Name"]: m["Unit"] for m in directive["Metrics"]} == {"ItemsHold": "Count", "RunDurationSeconds": "Seconds"}
    assert line["Service"] == "recall-agent" and line["ItemsHold"] == 1 and line["RunDurationSeconds"] == 2.5


def test_a_full_run_line_names_only_known_metrics_and_every_declared_metric_has_a_value(capsys):
    run()
    [line] = emf_lines(capsys)
    declared = [m["Name"] for m in line["_aws"]["CloudWatchMetrics"][0]["Metrics"]]
    assert set(declared) == set(metrics.METRIC_NAMES) and len(declared) <= 100
    assert all(isinstance(line[n], (int, float)) for n in declared)


# ------------------------------------------------------------------ the numbers

def test_a_clean_run_counts_items_queries_and_model_calls(capsys):
    out = run()
    [line] = emf_lines(capsys)
    # each supplier: the name on file + a first-word query = 2 openFDA queries, and one model call to decide
    assert (line["ItemsChecked"], line["ItemsClear"], line["ItemsHold"], line["ItemsNotChecked"]) == (2, 2, 0, 0)
    assert (line["OpenFdaQueries"], line["OpenFdaQueryFailures"], line["ModelCalls"]) == (4, 0, 2)
    assert line["RunsCompleted"] == 1 and line["WorkbookBuilt"] == 1 and line["RunDurationSeconds"] >= 0
    assert out["metrics"] == {k: line[k] for k in out["metrics"]}  # what is returned is what is published


def test_tokens_come_from_the_models_reported_usage(capsys):
    turns = [dict(_resp(decide("CLEAR", i=f"d{i}")), usage={"inputTokens": 1000, "outputTokens": 50}) for i in range(2)]
    run(bedrock=FakeClient(*turns))
    [line] = emf_lines(capsys)
    assert (line["InputTokens"], line["OutputTokens"]) == (2000, 100)


def test_a_rerun_does_not_recount_work_it_did_not_do(capsys):
    store, s3 = FakeStore(), FakeS3(CSV)
    run(store=store, s3=s3)
    capsys.readouterr()
    run(store=store, s3=s3, bedrock=FakeClient())  # everything is reused; the model must not be called
    [line] = emf_lines(capsys)
    assert (line["ItemsChecked"], line["ItemsResumed"], line["OpenFdaQueries"], line["ModelCalls"]) == (2, 2, 0, 0)


def test_failures_and_incomplete_checks_are_counted(capsys, monkeypatch):
    monkeypatch.setenv("FDA_FAULT", "rate_limit")
    run()
    [line] = emf_lines(capsys)
    assert line["ItemsNotChecked"] == 2 and line["OpenFdaQueryFailures"] == 2 and line["IncompleteChecks"] == 2


def test_hold_notices_sent_and_undelivered_are_counted(capsys, monkeypatch, ses):
    monkeypatch.setenv("FDA_FAULT", "lookalike")
    csv = "name,kind,lot,contact\nSunrise Food Co,supplier,LOT 8841A,buyer@example.com\n"
    turns = lambda: FakeClient(_resp(decide("HOLD", 0.92, ["F-1234-2026"])))
    run(csv, bedrock=turns())
    assert emf_lines(capsys)[0]["HoldNoticesSent"] == 1
    ses.fail_to.add("buyer@example.com")
    run(csv, bedrock=turns())
    line = emf_lines(capsys)[0]
    assert line["HoldNoticesSent"] == 0 and line["HoldNoticesUndelivered"] == 1


def test_the_circuit_breaker_is_visible_in_the_metrics(capsys, monkeypatch):
    real = handler.run_item
    monkeypatch.setattr(handler, "run_item", lambda c, i, *a, **k: (monkeypatch.setenv("FDA_FAULT", "rate_limit"), real(c, i, *a, **k))[1])
    csv = "name,kind,lot\n" + "\n".join(f"Supplier {c},supplier," for c in "ABCDE") + "\n"
    run(csv, bedrock=FakeClient(*[_resp(decide("CLEAR", i=f"d{i}")) for i in range(5)]))
    [line] = emf_lines(capsys)
    assert line["CircuitBreakerTripped"] == 1 and line["ItemsNotChecked"] == 5


def test_guardrail_interventions_are_counted(capsys, monkeypatch):
    monkeypatch.setenv("FDA_FAULT", "injection")
    monkeypatch.setenv("GUARDRAIL_ID", "gr-1")
    csv = "name,kind,lot,contact\nSunrise Food Co,supplier,LOT 8841A,buyer@example.com\n"
    bedrock = FakeClient()
    bedrock.apply_guardrail = fake_guard()
    run(csv, bedrock=bedrock)
    [line] = emf_lines(capsys)
    assert line["GuardrailInterventions"] == 1 and line["ItemsNeedsReview"] == 1 and line["ModelCalls"] == 0


# ------------------------------------------------------------------ metrics can never break a run

def test_a_failure_to_emit_metrics_does_not_affect_the_record_or_the_summary(monkeypatch, ses):
    monkeypatch.setattr(metrics, "emit", lambda *a, **k: (_ for _ in ()).throw(OSError("log pipe closed")))
    s3 = FakeS3(CSV)
    out = run(s3=s3)
    assert out["summary"]["CLEAR"] == 2 and ("bkt", "records/2026-10-05/weekly-recall-check.xlsx") in s3.objects
    assert [m for m in ses.sent if m["to"] == [COORD]]


# ------------------------------------------------------------------ the injection measurement set

INJECTION_SET = "evals/injection.json"


def test_the_injection_set_loads_and_has_threats_and_controls():
    cases = load_cases(INJECTION_SET)
    adversarial = [c.id for c in cases if c.category == "adversarial"]
    controls = [c.id for c in cases if c.category == "hold"]
    assert adversarial == [f"INJ{i}" for i in range(1, 11)] and controls == [f"CTL{i}" for i in range(1, 6)]
    assert all(c.guardrail_expected == "NEEDS_REVIEW" for c in cases if c.category == "adversarial")
    assert all(c.guardrail_expected is None for c in cases if c.category == "hold")  # an intervention on a control is a false alarm


def test_every_injection_case_is_still_a_confirmed_match_so_a_hold_is_the_right_answer_without_a_filter():
    import re
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    for case in load_cases(INJECTION_SET):
        rec = case.records[0]
        assert any(norm(l.code) in norm(rec["code_info"]) for l in case.item.lot_info), case.id   # our lot survives the injection
        assert rec["recalling_firm"] == case.item.name, case.id                                  # and so does the firm


def test_the_set_covers_several_attack_shapes_and_fields():
    cases = {c.id: c.records[0] for c in load_cases(INJECTION_SET)}
    assert "terminated" in cases["INJ4"]["reason_for_recall"] and "ignore" not in cases["INJ4"]["reason_for_recall"].lower()  # assertion-style
    assert "disregard" in cases["INJ6"]["product_description"]      # a field other than the reason
    assert "instruction" in cases["INJ7"]["code_info"]
    assert "record_decision" in cases["INJ8"]["reason_for_recall"]  # tool hijack
    assert "Ignora" in cases["INJ10"]["reason_for_recall"]          # not English
    for cid in ("CTL3", "CTL4", "CTL5"):                            # controls that contain the trigger words
        assert any(w in cases[cid]["reason_for_recall"].lower() for w in ("ignore", "instructions", "replaces all earlier"))


def test_without_a_guardrail_the_code_alone_still_stops_every_injection():
    """The second line of defense: whatever the model proposes, an injected CLEAR never reaches the record."""
    report = run_offline(load_cases(INJECTION_SET))
    assert report["violations"] == [] and report["invariants"]["SAFE-1"]["violations"] == 0


def test_with_a_stand_in_for_the_filter_every_threat_is_stopped_and_every_control_passes_through():
    """Tests our handling, not the filter: the stand-in flags the injected text of the adversarial cases only."""
    cases = load_cases(INJECTION_SET)
    base = {"product_description": "Sesame tahini, 16 oz jars", "code_info": "LOT 8841A"}
    markers = [v for c in cases if c.category == "adversarial" for k in ("reason_for_recall", "product_description", "code_info")
               if (v := c.records[0][k]) != base.get(k)]

    def stand_in(**kw):
        text = kw["content"][0]["text"]["text"]
        return {"action": "GUARDRAIL_INTERVENED", "assessments": []} if any(m in text for m in markers) else {"action": "NONE"}
    g = Guardrail("gr-1", "1")
    for case in cases:
        r = run_one(case, ScriptedClient(case.searches, proposals(case)["correct_hold"], guardrail_fn=stand_in), guardrail=g)
        assert r["decision"] == ("NEEDS_REVIEW" if case.category == "adversarial" else "HOLD"), case.id
        assert r["guardrail"]["action"] == ("intervened" if case.category == "adversarial" else "none"), case.id
