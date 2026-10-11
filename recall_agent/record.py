"""Render the weekly recall-check record.

The markdown layout is a placeholder until we have the coordinator's real log
format; the structure (what is shown per decision) is what matters. The JSON
twin carries the full audit trail for every item.
"""
from __future__ import annotations

import json
from collections import Counter

ORDER = ("HOLD", "NEEDS_REVIEW", "NOT_CHECKED", "CLEAR")
TITLES = {"HOLD": "Hold: action needed", "NEEDS_REVIEW": "Needs your review",
          "NOT_CHECKED": "Not checked (could not verify)", "CLEAR": "Checked, no FDA match found"}

NOTICE_LABELS = {"sent": "notice sent", "already_notified": "notice already sent earlier, not re-sent",
                 "failed": "NOTICE NOT SENT, contact the owner manually",
                 "no_contact": "NOTICE NOT SENT, no contact on file"}

SCOPE = ("Source: openFDA food enforcement reports, last 12 months. Covers FDA-regulated food only "
         "(not USDA-regulated meat, poultry or egg products), and reports can lag the recall itself. "
         "\"No FDA match found\" is not a safety determination.")


def summarize(results: list[dict]) -> dict:
    counts = Counter(r["decision"] for r in results)
    return {"total": len(results), **{d: counts.get(d, 0) for d in ORDER}}


def _queries(r: dict) -> str:
    calls = r["tool_calls"]
    return "; ".join(f'{c["field"]}="{c["term"]}" [{c["status"]}]' for c in calls) or "no query completed"


def _recall_lines(r: dict) -> list[str]:
    lines = []
    for m, rec in zip(r["matcher"]["matches"], r["evidence"]):
        lots = f" lots on hand affected: {', '.join(m['lot_overlap'])}" if m["lot_overlap"] else ""
        lines.append(f"  - {rec['classification']} {rec['recall_number']}: "
                     f"{rec.get('reason_for_recall', '')} ({rec.get('recalling_firm', '')}){lots}")
    return lines


def _entry(r: dict) -> list[str]:
    d, out = r["decision"], [f"- **{r['item']}** ({r['kind']})"]
    if d in ("HOLD", "NEEDS_REVIEW"):
        out += _recall_lines(r)
        if r.get("incomplete"):
            out.append("  - note: at least one query failed, so this check is incomplete")
    if d == "NEEDS_REVIEW":
        why = r["override_reason"] or (r["model_decision"] or {}).get("reasoning", "")
        out.append(f"  - why: {why}")
    if d == "NOT_CHECKED":
        errs = [c["error"] for c in r["tool_calls"] if c.get("error")]
        if r["model_error"]:
            errs.append(r["model_error"])
        out.append(f"  - why: {r['override_reason']}" + (f" ({'; '.join(errs)})" if errs else ""))
    out.append(f"  - queries: {_queries(r)}")
    return out


def render_markdown(run_date: str, results: list[dict], notes=(), notices=()) -> str:
    s = summarize(results)
    lines = [f"# Weekly recall check, {run_date}", "",
             f"{s['total']} items: {s['HOLD']} hold, {s['NEEDS_REVIEW']} need review, "
             f"{s['NOT_CHECKED']} not checked, {s['CLEAR']} no FDA match found.", "", SCOPE, ""]
    if notes:
        lines += ["**Run notes**", ""] + [f"- {n}" for n in notes] + [""]
    if notices:
        lines += ["**Hold notices**", ""]
        for n in notices:
            lines.append(f"- {n['item']}: " + NOTICE_LABELS[n["status"]]
                         + (f" ({n['to']})" if n["to"] and n["status"] in ("sent", "already_notified") else "")
                         + (f" [{n['error']}]" if n["error"] else ""))
        lines.append("")
    for d in ORDER:
        group = [r for r in results if r["decision"] == d]
        if group:
            lines += [f"## {TITLES[d]} ({len(group)})", ""]
            for r in group:
                lines += _entry(r)
            lines.append("")
    return "\n".join(lines)


def render_json(run_date: str, results: list[dict], notes=(), notices=()) -> str:
    return json.dumps({"run_date": run_date, "summary": summarize(results), "scope": SCOPE,
                       "notes": list(notes), "notices": list(notices),
                       "results": results}, indent=2)
