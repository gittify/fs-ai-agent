"""Run metrics, published as CloudWatch Embedded Metric Format (EMF) log lines.

One JSON line per run is printed to stdout; CloudWatch Logs turns it into custom metrics with no
PutMetricData call and no extra IAM permission. Metrics are best-effort: a failure to emit must never
affect the compliance record, so the handler wraps emission and carries on.

Counts of queries and tokens cover only items actually run in this invocation. Items reused from an
earlier run (resume) are not re-counted, so a rerun does not double the cost figures.
"""
from __future__ import annotations

import json
import sys
import time

NAMESPACE = "RecallAgent"
SERVICE = "recall-agent"
UNITS = {
    "ItemsChecked": "Count", "ItemsHold": "Count", "ItemsClear": "Count", "ItemsNeedsReview": "Count",
    "ItemsNotChecked": "Count", "ItemsResumed": "Count", "IncompleteChecks": "Count",
    "HoldNoticesSent": "Count", "HoldNoticesAlreadySent": "Count", "HoldNoticesUndelivered": "Count",
    "OpenFdaQueries": "Count", "OpenFdaQueryFailures": "Count", "TruncatedQueries": "Count",
    "ModelCalls": "Count", "InputTokens": "Count", "OutputTokens": "Count",
    "GuardrailInterventions": "Count", "GuardrailErrors": "Count",
    "CircuitBreakerTripped": "Count", "WorkbookBuilt": "Count", "RunsCompleted": "Count",
    "RunDurationSeconds": "Seconds",
}
METRIC_NAMES = tuple(UNITS)


def build(results: list[dict], fresh: list[dict], notices: list[dict], resumed: int, notes: list[str],
          duration_s: float, workbook_built: bool) -> dict[str, float]:
    by = lambda d: sum(1 for r in results if r["decision"] == d)
    calls = [c for r in fresh for c in r.get("tool_calls", [])]
    guard = [r.get("guardrail", {}).get("action") for r in fresh]
    usage = lambda k: sum(r.get("usage", {}).get(k, 0) for r in fresh)
    notice = lambda s: sum(1 for n in notices if n["status"] == s)
    return {
        "ItemsChecked": len(results), "ItemsHold": by("HOLD"), "ItemsClear": by("CLEAR"),
        "ItemsNeedsReview": by("NEEDS_REVIEW"), "ItemsNotChecked": by("NOT_CHECKED"), "ItemsResumed": resumed,
        "IncompleteChecks": sum(1 for r in results if r.get("incomplete")),
        "HoldNoticesSent": notice("sent"), "HoldNoticesAlreadySent": notice("already_notified"),
        "HoldNoticesUndelivered": notice("failed") + notice("no_contact"),
        "OpenFdaQueries": sum(1 for c in calls if c["status"] in ("clear", "matches", "error")),
        "OpenFdaQueryFailures": sum(1 for c in calls if c["status"] == "error"),
        "TruncatedQueries": sum(1 for c in calls if c.get("truncated")),
        "ModelCalls": usage("model_calls"), "InputTokens": usage("input_tokens"), "OutputTokens": usage("output_tokens"),
        "GuardrailInterventions": guard.count("intervened"), "GuardrailErrors": guard.count("error"),
        "CircuitBreakerTripped": 1 if any(n.startswith("Circuit breaker") for n in notes) else 0,
        "WorkbookBuilt": 1 if workbook_built else 0, "RunsCompleted": 1,
        "RunDurationSeconds": round(duration_s, 2),
    }


def emit(metrics: dict[str, float], stream=None, now: float | None = None) -> str:
    """Print one EMF line. Returns it, for tests."""
    line = json.dumps({
        "_aws": {"Timestamp": int((now if now is not None else time.time()) * 1000),
                 "CloudWatchMetrics": [{"Namespace": NAMESPACE, "Dimensions": [["Service"]],
                                        "Metrics": [{"Name": k, "Unit": UNITS[k]} for k in metrics]}]},
        "Service": SERVICE, **metrics})
    print(line, file=stream or sys.stdout, flush=True)
    return line
