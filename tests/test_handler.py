"""Offline tests for the Lambda handler: fake S3, fake Bedrock, injected openFDA faults."""
import io

import pytest

from recall_agent import handler
from recall_agent.inventory import parse_inventory
from tests.test_failure_paths import FakeClient, _resp, decide, search

CSV = """name,kind,lots,aliases,contact
Sunrise Foods Inc,supplier,LOT 7710B,Sunrise Foods,purchasing@example.com
tahini,ingredient,T-2209-14,sesame paste,warehouse@example.com
Acme Spice Traders,supplier,,,
"""


class FakeS3:
    def __init__(self, csv_text):
        self.objects = {("bkt", "inventory/items.csv"): csv_text.encode()}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[(Bucket, Key)] = Body


class Ctx:
    def __init__(self, ms):
        self.ms = ms

    def get_remaining_time_in_millis(self):
        return self.ms


EVENT = {"bucket": "bkt", "run_date": "2026-10-05"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("FDA_BACKOFF_BASE", "0")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "test-model")


def clear_turns(n):
    out = []
    for i in range(n):
        out += [_resp(search(i=f"s{i}")), _resp(decide("CLEAR", i=f"d{i}"))]
    return out


def test_happy_path_writes_markdown_and_json(monkeypatch):
    monkeypatch.setenv("FDA_FAULT", "not_found")
    s3 = FakeS3(CSV)
    out = handler.lambda_handler(EVENT, None, s3=s3, bedrock=FakeClient(*clear_turns(3)))
    assert out["summary"]["CLEAR"] == 3
    md = s3.objects[("bkt", "records/2026-10-05/weekly-recall-check.md")].decode()
    assert "3 items" in md and "Sunrise Foods Inc" in md and "not a safety determination" in md
    assert ("bkt", "records/2026-10-05/weekly-recall-check.json") in s3.objects


def test_rate_limited_item_is_not_checked_in_the_record(monkeypatch):
    monkeypatch.setenv("FDA_FAULT", "rate_limit")
    monkeypatch.setenv("FDA_FAULT_TERM", "Sunrise")
    s3 = FakeS3(CSV)
    # item 1 searches Sunrise (faulted); items 2 and 3 get clean 404s via real path being faulted off
    # so script them to see no_matches by faulting only Sunrise and using not_found for the rest
    monkeypatch.setattr(handler, "run_item", _selective(handler.run_item))
    out = handler.lambda_handler(EVENT, None, s3=s3, bedrock=FakeClient(*clear_turns(3)))
    assert out["summary"]["NOT_CHECKED"] >= 1
    md = s3.objects[("bkt", "records/2026-10-05/weekly-recall-check.md")].decode()
    assert "Not checked (could not verify)" in md and "gave up after 4 attempts" in md


def _selective(real):
    # Non-Sunrise items would hit the live API; make them clean 404s instead.
    import os

    def wrapped(client, item, *a, **k):
        if "sunrise" not in item.name.lower():
            os.environ["FDA_FAULT"] = "not_found"
            os.environ["FDA_FAULT_TERM"] = ""
        else:
            os.environ["FDA_FAULT"] = "rate_limit"
            os.environ["FDA_FAULT_TERM"] = "Sunrise"
        return real(client, item, *a, **k)
    return wrapped


def test_out_of_time_marks_remaining_items_not_checked(monkeypatch):
    s3 = FakeS3(CSV)
    out = handler.lambda_handler(EVENT, Ctx(1_000), s3=s3, bedrock=FakeClient())
    assert out["summary"]["NOT_CHECKED"] == 3
    md = s3.objects[("bkt", "records/2026-10-05/weekly-recall-check.md")].decode()
    assert "ran out of time" in md


def test_unexpected_exception_in_one_item_does_not_sink_the_run(monkeypatch):
    monkeypatch.setenv("FDA_FAULT", "not_found")
    calls = {"n": 0}
    real = handler.run_item

    def flaky(client, item, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return real(client, item, *a, **k)

    monkeypatch.setattr(handler, "run_item", flaky)
    out = handler.lambda_handler(EVENT, None, s3=FakeS3(CSV), bedrock=FakeClient(*clear_turns(2)))
    assert out["summary"]["CLEAR"] == 2 and out["summary"]["NOT_CHECKED"] == 1


def test_bad_inventory_fails_before_any_check_runs():
    bad = "name,kind\nSunrise,vendor\n,supplier\n"
    with pytest.raises(ValueError) as e:
        handler.lambda_handler(EVENT, None, s3=FakeS3(bad), bedrock=FakeClient())
    assert "row 2" in str(e.value) and "row 3" in str(e.value)


def test_inventory_parsing_splits_lots_aliases_contact():
    items = parse_inventory(CSV)
    assert items[0].lots == ["LOT 7710B"] and items[0].contact == "purchasing@example.com"
    assert items[1].aliases == ["sesame paste"] and items[2].lots == []
