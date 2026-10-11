"""Lambda entry point (EventBridge Scheduler fires this weekly).

Environment:
    DATA_BUCKET       S3 bucket (or pass "bucket" in the event)
    INVENTORY_KEY     default inventory/items.csv
    BEDROCK_MODEL_ID  Bedrock model or inference-profile ID
    SENDER_EMAIL      SES-verified sender address
    COORDINATOR_EMAIL gets the weekly summary and is CC'd on every hold notice
    SEND_WHEN_DEGRADED  "true" (default) or "false": send hold notices even if the state
                      table is unavailable (risking a duplicate) or hold them back
Event overrides (all optional): bucket, inventory_key, run_date (YYYY-MM-DD), sender, coordinator.

Failure paths owned here:
    - bad inventory CSV            -> raise before any check runs
    - one item blows up            -> that item is NOT_CHECKED, the run continues
    - Lambda time budget running out -> remaining items are NOT_CHECKED, not dropped
    - openFDA down for several items in a row -> circuit breaker: the rest are NOT_CHECKED without
      being queried (each failing item otherwise burns seconds of backoff), and a rerun retries them
    - state table missing or down  -> run anyway, but say so in the record's notes
    - rerun with the same run_date -> final results are reused, NOT_CHECKED items are retried
    - hold notice undeliverable    -> flagged in the record and summary as manual action
    - summary email fails          -> the invocation fails (after the record is saved)
    - workbook cannot be built     -> markdown + JSON are still written, and the run notes say so

Also reads STATE_TABLE (DynamoDB table name).
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import metrics, workbook
from .agent import make_client, run_item
from .inventory import parse_inventory_with_notes
from .matcher import Item
from .notify import make_ses, send_hold_notices, send_summary
from .record import render_json, render_markdown, summarize
from .state import DynamoStore, item_key

MIN_REMAINING_MS = 90_000  # one worst-case item (several model turns + retries)
BREAKER_THRESHOLD = 3  # consecutive items that could not be queried before we stop calling openFDA


def not_checked(item: Item, reason: str) -> dict:
    return {"item": item.name, "kind": item.kind, "decision": "NOT_CHECKED",
            "override_reason": reason, "model_decision": None,
            "matcher": {"verdict": "none", "matches": []}, "evidence": [],
            "tool_calls": [], "model_error": None}


def _out_of_time(context) -> bool:
    return context is not None and context.get_remaining_time_in_millis() < MIN_REMAINING_MS


@dataclass
class RunOutcome:
    results: list = field(default_factory=list)
    fresh: list = field(default_factory=list)  # results produced in THIS invocation (not reused from state)
    resumed: int = 0
    notes: list = field(default_factory=list)


def _tool_unavailable(result: dict) -> bool:
    """The item could not be verified because openFDA queries failed (not because of the model)."""
    return result["decision"] == "NOT_CHECKED" and any(c["status"] == "error" for c in result["tool_calls"])


def run_all(items: list[Item], bedrock, context=None, store=None, run_id: str = "") -> RunOutcome:
    out, done = RunOutcome(), {}
    if store is None:
        out.notes.append("No state table configured: reruns cannot resume and duplicate-notice "
                         "protection is off.")
    else:
        try:
            done = store.load_final(run_id)
        except Exception as e:
            out.notes.append(f"State store unavailable ({type(e).__name__}): reruns cannot resume "
                             "and duplicate-notice protection is off.")
            store = None

    unavailable_streak, breaker_open = 0, False
    for item in items:
        key = item_key(item)
        if key in done:
            out.results.append(done[key])
            out.resumed += 1
            continue
        if _out_of_time(context):
            res = not_checked(item, "run ran out of time before this item was reached")
        elif breaker_open:
            res = not_checked(item, f"openFDA appears to be unavailable ({BREAKER_THRESHOLD} items in a row could "
                                    "not be queried), so this item was skipped; rerun later to check it")
        else:
            try:
                res = run_item(bedrock, item)
                unavailable_streak = unavailable_streak + 1 if _tool_unavailable(res) else 0
                if unavailable_streak >= BREAKER_THRESHOLD:
                    breaker_open = True
                    out.notes.append(f"Circuit breaker: {BREAKER_THRESHOLD} items in a row could not be queried, "
                                     "so the remaining items were not attempted. Rerun later; finished items are reused.")
            except Exception as e:  # one bad item must not sink the whole record
                res = not_checked(item, f"unexpected error: {type(e).__name__}: {e}")
        out.results.append(res)
        out.fresh.append(res)
        if store is not None:
            try:
                store.save_result(run_id, key, res)
            except Exception as e:
                out.notes.append(f"State store write failed ({type(e).__name__}); later items were "
                                 "not saved, so a rerun will redo them.")
                store = None

    if out.resumed:
        out.notes.append(f"{out.resumed} item(s) reused from an earlier run with the same date; "
                         "not re-queried.")
    return out


FAULT_MODES = ("rate_limit", "timeout", "garbage", "malformed", "not_found", "lookalike", "injection")


@contextmanager
def _demo_fault(event):
    """Inject an openFDA fault for this one invocation (demo rehearsal only).

    Event: {"demo_fault": {"mode": "rate_limit", "term": "Globex"}}. Refused loudly unless the stack
    was deployed with demo_mode=true. The environment is always restored afterwards: a warm Lambda
    container is reused, and a leaked fault would corrupt the next scheduled run.
    """
    fault = event.get("demo_fault")
    if not fault:
        yield
        return
    if os.getenv("DEMO_MODE", "").lower() != "true":
        raise ValueError("demo_fault is only honored when the stack was deployed with demo_mode=true")
    if fault.get("mode") not in FAULT_MODES:
        raise ValueError(f"demo_fault mode must be one of {FAULT_MODES}")
    keys = ("FDA_FAULT", "FDA_FAULT_TERM")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["FDA_FAULT"], os.environ["FDA_FAULT_TERM"] = fault["mode"], fault.get("term", "")
    try:
        yield
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def lambda_handler(event, context, s3=None, bedrock=None, store=None, ses=None):
    event = event or {}
    with _demo_fault(event):
        return _run(event, context, s3, bedrock, store, ses)


def _run(event, context, s3, bedrock, store, ses):
    started = time.monotonic()
    bucket = event.get("bucket") or os.environ["DATA_BUCKET"]
    inv_key = event.get("inventory_key") or os.getenv("INVENTORY_KEY", "inventory/items.csv")
    sender = event.get("sender") or os.environ["SENDER_EMAIL"]  # fail before doing any work
    coordinator = event.get("coordinator") or os.environ["COORDINATOR_EMAIL"]
    run_date = event.get("run_date") or datetime.now(timezone.utc).date().isoformat()

    if s3 is None:
        import boto3
        s3 = boto3.client("s3")
    bedrock = bedrock or make_client()
    ses = ses or make_ses()
    if store is None and os.getenv("STATE_TABLE"):
        store = DynamoStore.from_env()

    text = s3.get_object(Bucket=bucket, Key=inv_key)["Body"].read().decode("utf-8")
    items, inventory_notes = parse_inventory_with_notes(text)
    outcome = run_all(items, bedrock, context, store, run_date)
    outcome.notes[:0] = inventory_notes
    results = outcome.results

    notices = [n.to_dict() for n in send_hold_notices(
        results, items, store, ses, sender, coordinator, run_date,
        send_when_degraded=os.getenv("SEND_WHEN_DEGRADED", "true").lower() != "false")]

    prefix = f"records/{run_date}/weekly-recall-check"
    try:
        xlsx = workbook.build_workbook(workbook.load_bundled_template(), run_date, results, notices,
                                       outcome.notes)
    except Exception as e:  # missing dependency, edited template, anything: never lose the run
        xlsx = None
        outcome.notes.append(f"Workbook could not be generated ({type(e).__name__}: {e}). "
                             "Use the markdown and JSON record for this run.")
    if xlsx is not None:
        s3.put_object(Bucket=bucket, Key=f"{prefix}.xlsx", Body=xlsx,
                      ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    s3.put_object(Bucket=bucket, Key=f"{prefix}.md",
                  Body=render_markdown(run_date, results, outcome.notes, notices).encode(),
                  ContentType="text/markdown")
    s3.put_object(Bucket=bucket, Key=f"{prefix}.json",
                  Body=render_json(run_date, results, outcome.notes, notices).encode(),
                  ContentType="application/json")

    run_metrics = metrics.build(results, outcome.fresh, notices, outcome.resumed, outcome.notes,
                                time.monotonic() - started, xlsx is not None)
    try:  # best effort: metrics must never affect the compliance record
        metrics.emit(run_metrics)
    except Exception:
        pass

    # Last on purpose: the record is already saved if this raises.
    send_summary(ses, sender, coordinator, run_date, results, notices, outcome.notes,
                 f"s3://{bucket}/{prefix}.{'xlsx' if xlsx is not None else 'md'}")
    return {"run_date": run_date, "summary": summarize(results), "resumed": outcome.resumed,
            "notices": notices, "notes": outcome.notes, "record_key": f"{prefix}.md",
            "workbook_key": f"{prefix}.xlsx" if xlsx is not None else None, "metrics": run_metrics}
