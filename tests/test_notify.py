"""SES notices: hold emails, the always-sent summary, and what happens when sending breaks."""
import pytest

from recall_agent import handler
from recall_agent.notify import _send
from recall_agent.state import notice_key
from tests.conftest import COORD
from tests.test_failure_paths import FakeClient, _resp, decide, search
from tests.test_handler import EVENT, FakeS3

HOLD_CSV = "name,kind,lots,aliases,contact\nSunrise Food Co,supplier,LOT 8841A,,purchasing@example.com\n"
NO_CONTACT_CSV = "name,kind,lots,aliases,contact\nSunrise Food Co,supplier,LOT 8841A,,\n"
CLEAN_CSV = "name,kind,lots,aliases,contact\nAcme Spice Traders,supplier,,,\n"


class FakeStore:
    def __init__(self):
        self.rows, self.notices = {}, {}

    def load_final(self, run_id):
        return {k: v for (r, k), v in self.rows.items() if r == run_id and v["decision"] != "NOT_CHECKED"}

    def save_result(self, run_id, key, result, now=None):
        self.rows[(run_id, key)] = result

    def claim_notice(self, item, recalls, now=None):
        k = notice_key(item, recalls)
        if self.notices.get(k) in (None, "FAILED"):
            self.notices[k] = "PENDING"
            return True
        return False

    def finish_notice(self, item, recalls, ok, now=None):
        self.notices[notice_key(item, recalls)] = "SENT" if ok else "FAILED"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("FDA_BACKOFF_BASE", "0")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "test-model")
    monkeypatch.setenv("FDA_FAULT", "lookalike")


def hold_turns():
    return [_resp(search("Sunrise Food Co")), _resp(decide("HOLD", 0.92, ["F-1234-2026"]))]


def go(csv, bedrock, store=None, s3=None):
    s3 = s3 or FakeS3(csv)
    return handler.lambda_handler(EVENT, None, s3=s3, bedrock=bedrock, store=store), s3


def holds(ses):
    return [m for m in ses.sent if m["subject"].startswith("HOLD request")]


def summary(ses):
    return [m for m in ses.sent if m["to"] == [COORD]][-1]


def test_hold_notice_goes_to_contact_with_coordinator_cc_and_evidence(ses):
    out, _ = go(HOLD_CSV, FakeClient(*hold_turns()), FakeStore())
    [mail] = holds(ses)
    assert mail["to"] == ["purchasing@example.com"] and mail["cc"] == [COORD]
    assert "F-1234-2026" in mail["subject"] and "LOT 8841A" in mail["body"] and "Class I" in mail["body"]
    assert out["notices"][0]["status"] == "sent"
    assert summary(ses)["subject"].startswith("[Action needed] Recall check 2026-10-05: 1 hold")


def test_rerun_does_not_resend_the_hold_notice(ses):
    store = FakeStore()
    go(HOLD_CSV, FakeClient(*hold_turns()), store)
    out, s3 = go(HOLD_CSV, FakeClient(), store)  # no model calls needed: result is reused
    assert len(holds(ses)) == 1 and out["notices"][0]["status"] == "already_notified"
    assert "already sent earlier" in summary(ses)["body"]
    assert "already sent earlier" in s3.objects[("bkt", "records/2026-10-05/weekly-recall-check.md")].decode()


def test_failed_send_is_flagged_loudly_then_retried_next_run(ses):
    store = FakeStore()
    ses.fail_to.add("purchasing@example.com")
    out, s3 = go(HOLD_CSV, FakeClient(*hold_turns()), store)
    assert out["notices"][0]["status"] == "failed" and holds(ses) == []
    body = summary(ses)["body"]
    assert "MANUAL ACTION REQUIRED" in body and "Sunrise Food Co" in body and "not verified" in body
    assert "NOTICE NOT SENT" in s3.objects[("bkt", "records/2026-10-05/weekly-recall-check.md")].decode()

    ses.fail_to.clear()
    out2, _ = go(HOLD_CSV, FakeClient(), store)
    assert out2["notices"][0]["status"] == "sent" and len(holds(ses)) == 1


def test_hold_with_no_contact_is_manual_action_not_silence(ses):
    out, _ = go(NO_CONTACT_CSV, FakeClient(*hold_turns()), FakeStore())
    assert out["notices"][0]["status"] == "no_contact" and holds(ses) == []
    assert "MANUAL ACTION REQUIRED" in summary(ses)["body"]


def test_state_down_sends_anyway_by_default_and_can_be_flipped(ses, monkeypatch):
    out, _ = go(HOLD_CSV, FakeClient(*hold_turns()), store=None)
    assert out["notices"][0]["status"] == "sent" and "duplicate-notice protection is off" in summary(ses)["body"]

    ses.sent.clear()
    monkeypatch.setenv("SEND_WHEN_DEGRADED", "false")
    out, _ = go(HOLD_CSV, FakeClient(*hold_turns()), store=None)
    assert out["notices"][0]["status"] == "failed" and holds(ses) == []
    assert "MANUAL ACTION REQUIRED" in summary(ses)["body"]


def test_clean_week_still_sends_a_summary_as_a_heartbeat(ses, monkeypatch):
    monkeypatch.setenv("FDA_FAULT", "not_found")
    go(CLEAN_CSV, FakeClient(_resp(search("Acme Spice Traders")), _resp(decide("CLEAR"))), FakeStore())
    assert len(ses.sent) == 1
    assert ses.sent[0]["subject"] == "Recall check 2026-10-05: all clear (1 items)"


def test_not_checked_item_makes_the_summary_say_action_needed(ses, monkeypatch):
    monkeypatch.setenv("FDA_FAULT", "rate_limit")
    go(CLEAN_CSV, FakeClient(_resp(search("Acme Spice Traders")), _resp(decide("CLEAR"))), FakeStore())
    s = summary(ses)
    assert s["subject"].startswith("[Action needed]") and "1 not checked" in s["subject"]
    assert "NOT the same as clear" in s["body"]


def test_summary_failure_raises_but_record_is_already_saved(ses):
    ses.fail_to.add(COORD)
    s3 = FakeS3(HOLD_CSV)
    with pytest.raises(RuntimeError):
        handler.lambda_handler(EVENT, None, s3=s3, bedrock=FakeClient(*hold_turns()), store=FakeStore())
    assert ("bkt", "records/2026-10-05/weekly-recall-check.md") in s3.objects


def test_subject_cannot_be_used_for_header_injection(ses):
    _send(ses, "a@example.com", ["b@example.com"], [], "Hold: x\nBcc: evil@example.com", "body")
    assert "\n" not in ses.sent[0]["subject"]
