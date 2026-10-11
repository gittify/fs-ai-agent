"""The gated demo fault hook and the demo state reset script."""
import os
import sys

import boto3
import pytest
from moto import mock_aws

from recall_agent import handler
from tests.conftest import COORD
from tests.test_failure_paths import FakeClient, _resp, decide, search
from tests.test_handler import EVENT, FakeS3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from reset_demo_state import main as reset_main, reset  # noqa: E402

CSV = "name,kind,lot,contact\nSunrise Food Co,supplier,LOT 8841A,buyer@example.com\n"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("FDA_BACKOFF_BASE", "0")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "test-model")
    for k in ("FDA_FAULT", "FDA_FAULT_TERM", "DEMO_MODE"):
        monkeypatch.delenv(k, raising=False)


def run(event, monkeypatch=None):
    s3 = FakeS3(CSV)
    model = FakeClient(_resp(search("Sunrise Food Co")), _resp(decide("CLEAR")))
    return handler.lambda_handler({**EVENT, **event}, None, s3=s3, bedrock=model, store=None), s3


def test_fault_is_refused_loudly_unless_demo_mode_is_on(ses):
    with pytest.raises(ValueError) as e:
        run({"demo_fault": {"mode": "rate_limit"}})
    assert "demo_mode=true" in str(e.value) and ses.sent == []  # nothing ran, nothing was sent


def test_unknown_fault_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    with pytest.raises(ValueError):
        run({"demo_fault": {"mode": "meteor_strike"}})


def test_injected_fault_applies_to_the_run_and_is_restored_afterwards(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    out, _ = run({"demo_fault": {"mode": "rate_limit", "term": "Sunrise"}})
    assert out["summary"]["NOT_CHECKED"] == 1
    assert "FDA_FAULT" not in os.environ and "FDA_FAULT_TERM" not in os.environ  # no leak into a warm container


def test_the_environment_is_restored_even_when_the_run_fails(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("FDA_FAULT", "not_found")  # a pre-existing value must come back unchanged
    with pytest.raises(Exception):
        handler.lambda_handler({**EVENT, "demo_fault": {"mode": "garbage"}}, None, s3=FakeS3("bad,csv\n"),
                               bedrock=FakeClient(), store=None)
    assert os.environ["FDA_FAULT"] == "not_found"


def test_without_a_fault_nothing_changes(monkeypatch):
    monkeypatch.setenv("FDA_FAULT", "not_found")
    out, _ = run({})
    assert out["summary"]["CLEAR"] == 1


# ---------------------------------------------------------------- reset script

@pytest.fixture
def table(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        t = ddb.create_table(TableName="state", BillingMode="PAY_PER_REQUEST",
                             KeySchema=[{"AttributeName": "run_id", "KeyType": "HASH"},
                                        {"AttributeName": "item_key", "KeyType": "RANGE"}],
                             AttributeDefinitions=[{"AttributeName": "run_id", "AttributeType": "S"},
                                                   {"AttributeName": "item_key", "AttributeType": "S"}])
        for run_id in ("2026-10-13", "2026-10-14", "2026-12-01", "NOTICES"):
            for k in ("supplier#a", "supplier#b"):
                t.put_item(Item={"run_id": run_id, "item_key": k, "decision": "CLEAR"})
        yield t


def keys(t):
    return {(i["run_id"]) for i in t.scan()["Items"]}


def test_reset_deletes_only_the_listed_dates_and_the_notice_ledger(table):
    assert reset(table, ["2026-10-13", "2026-10-14"]) == 6
    assert keys(table) == {"2026-12-01"}  # an unrelated run is untouched


def test_reset_can_keep_the_notice_ledger(table):
    reset(table, ["2026-10-13"], include_notices=False)
    assert keys(table) == {"2026-10-14", "2026-12-01", "NOTICES"}


def test_reset_script_refuses_without_confirmation(table, capsys):
    assert reset_main(["--table", "state", "--run-dates", "2026-10-13"]) == 2
    assert len(table.scan()["Items"]) == 8  # nothing deleted
