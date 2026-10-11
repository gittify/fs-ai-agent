"""Hold notices and the weekly summary email, sent through SES (sesv2).

Two kinds of email:

  Hold notice   one per HOLD item, to that item's contact, coordinator on CC.
                Sent at most once per (item, recall) using the state ledger:
                claim -> send -> finish(ok). A failed send is retried next run.

  Summary       one per run, to the coordinator, ALWAYS, even when everything
                is clear. It doubles as a heartbeat: a Monday with no email
                means the agent did not run.

Failure behavior:
  no contact on file            -> status no_contact, listed under manual action
  SES rejects / throttles       -> status failed, listed under manual action
  state store down              -> send anyway by default (a duplicate hold is
                                   cheap, a missed one is not); set
                                   send_when_degraded=False to hold back instead
  summary email fails           -> raises, so the Lambda invocation fails visibly
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .record import _recall_lines, summarize
from .state import item_key, key_from


@dataclass
class NoticeOutcome:
    item: str
    status: str  # sent | already_notified | failed | no_contact
    to: str = ""
    recalls: list = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def make_ses():
    import boto3
    return boto3.client("sesv2")


def _oneline(s) -> str:
    return " ".join(str(s).split())  # no header injection via names containing newlines


def _addresses(contact: str) -> list[str]:
    return [a for a in re.split(r"[;,\s]+", contact or "") if a]


def _send(ses, sender: str, to: list[str], cc: list[str], subject: str, body: str) -> None:
    dest = {"ToAddresses": to}
    if cc:
        dest["CcAddresses"] = cc
    ses.send_email(FromEmailAddress=sender, Destination=dest,
                   Content={"Simple": {"Subject": {"Data": _oneline(subject), "Charset": "UTF-8"},
                                       "Body": {"Text": {"Data": body, "Charset": "UTF-8"}}}})


def hold_email(result: dict, run_date: str, coordinator: str) -> tuple[str, str]:
    strong = [(m, rec) for m, rec in zip(result["matcher"]["matches"], result["evidence"])
              if m["level"] == "strong"]
    lines = [f"HOLD REQUEST: {result['item']} ({result['kind']})", f"Weekly recall check, {run_date}", "",
             "The FDA enforcement database lists a recall that matches this item and lot code(s) "
             "we have in inventory.", ""]
    for m, rec in strong:
        lines += [f"Recall: {rec['classification']} {rec['recall_number']} (status: {rec.get('status')})",
                  f"Recalling firm: {rec.get('recalling_firm')}",
                  f"Product: {rec.get('product_description')}",
                  f"Lot/code info: {rec.get('code_info')}",
                  f"Reason: {rec.get('reason_for_recall')}",
                  f"Distribution: {rec.get('distribution_pattern')}",
                  f"FDA report date: {rec.get('report_date')}",
                  f"Our lots affected: {', '.join(m['lot_overlap'])}", ""]
    lines += ["Please place the affected lots on hold, do not ship or use them, and reply to "
              f"{coordinator} to confirm.", "",
              "This is an automated message from the weekly recall check. The quality coordinator "
              "is copied. Match basis: recalling firm or product plus lot code, found in the "
              "FDA enforcement report, not a safety determination."]
    recalls = ", ".join(m["recall_number"] for m, _ in strong)
    return f"HOLD request: {result['item']} ({recalls})", "\n".join(lines)


def send_hold_notices(results, items, store, ses, sender, coordinator, run_date,
                      send_when_degraded: bool = True) -> list[NoticeOutcome]:
    by_key = {item_key(i): i for i in items}
    outcomes = []
    for r in results:
        if r["decision"] != "HOLD":
            continue
        item = by_key.get(key_from(r["item"], r["kind"]))
        recalls = sorted(m["recall_number"] for m in r["matcher"]["matches"] if m["level"] == "strong")
        to = _addresses(item.contact) if item else []
        if not to:
            outcomes.append(NoticeOutcome(r["item"], "no_contact", recalls=recalls,
                                          error="no contact email on file for this item"))
            continue

        claimed_in_store = False
        if store is not None:
            try:
                if not store.claim_notice(item, recalls):
                    outcomes.append(NoticeOutcome(r["item"], "already_notified", ", ".join(to), recalls))
                    continue
                claimed_in_store = True
            except Exception as e:
                if not send_when_degraded:
                    outcomes.append(NoticeOutcome(r["item"], "failed", ", ".join(to), recalls,
                                                  f"state store unavailable, not sent ({type(e).__name__})"))
                    continue
        elif not send_when_degraded:
            outcomes.append(NoticeOutcome(r["item"], "failed", ", ".join(to), recalls,
                                          "no state store, not sent"))
            continue

        try:
            subject, body = hold_email(r, run_date, coordinator)
            _send(ses, sender, to, [coordinator], subject, body)
            ok, err = True, ""
        except Exception as e:
            ok, err = False, f"{type(e).__name__}: {e}"
        if claimed_in_store:
            try:
                store.finish_notice(item, recalls, ok)
            except Exception:
                pass  # worst case: the stale claim expires and a duplicate is sent. Acceptable.
        outcomes.append(NoticeOutcome(r["item"], "sent" if ok else "failed", ", ".join(to), recalls, err))
    return outcomes


def summary_email(run_date, results, notices, notes, record_location) -> tuple[str, str]:
    c = summarize(results)
    review = [r for r in results if r["decision"] == "NEEDS_REVIEW"]
    unchecked = [r for r in results if r["decision"] == "NOT_CHECKED"]
    manual = [n for n in notices if n["status"] in ("failed", "no_contact")]
    all_clear = not (c["HOLD"] or review or unchecked)

    parts = [f"{n} {label}" for n, label in ((c["HOLD"], "hold"), (len(review), "need review"),
                                             (len(unchecked), "not checked")) if n]
    subject = ("" if all_clear else "[Action needed] ") + \
        f"Recall check {run_date}: " + (f"all clear ({c['total']} items)" if all_clear else ", ".join(parts))

    lines = [f"Weekly recall check, {run_date}: {c['total']} items. {c['HOLD']} hold, "
             f"{len(review)} need review, {len(unchecked)} not checked, {c['CLEAR']} no FDA match found.", ""]
    if manual:
        lines += ["MANUAL ACTION REQUIRED: these hold notices were NOT delivered. Contact the owner yourself:"]
        lines += [f"- {n['item']} ({', '.join(n['recalls'])}): {n['error']}" for n in manual] + [""]
    sent = [n for n in notices if n["status"] in ("sent", "already_notified")]
    if sent:
        lines += ["Hold notices:"]
        lines += [f"- {n['item']} -> {n['to']}: " + ("sent" if n["status"] == "sent"
                                                    else "already sent earlier, not re-sent") for n in sent] + [""]
    if review:
        lines += ["Needs your review (the agent did not clear or hold these on its own):"]
        for r in review:
            why = r["override_reason"] or (r["model_decision"] or {}).get("reasoning", "")
            lines += [f"- {r['item']}: {why}" + (" (check incomplete: a query failed)" if r.get("incomplete") else "")]
            lines += _recall_lines(r)
        lines.append("")
    if unchecked:
        lines += ["Not checked (could not verify; NOT the same as clear):"]
        for r in unchecked:
            errs = [c_["error"] for c_ in r["tool_calls"] if c_.get("error")]
            lines.append(f"- {r['item']}: {r['override_reason']}" + (f" ({'; '.join(errs)})" if errs else ""))
        lines.append("")
    if notes:
        lines += ["Run notes:"] + [f"- {n}" for n in notes] + [""]
    lines += [f"Full record: {record_location}",
              "Scope: FDA-regulated food enforcement reports only (not USDA meat, poultry or egg). "
              "\"No FDA match found\" is not a safety determination."]
    return subject, "\n".join(lines)


def send_summary(ses, sender, coordinator, run_date, results, notices, notes, record_location) -> None:
    subject, body = summary_email(run_date, results, notices, notes, record_location)
    _send(ses, sender, [coordinator], [], subject, body)  # let failures raise
