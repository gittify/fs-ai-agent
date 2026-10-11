"""Run the evals.

    python -m evals.run offline                      # guardrail eval, no AWS needed
    python -m evals.run live --model-id <id> [--model-id <id2>] [--repeats 3] [--guardrail-id <id> [--guardrail-version 1]]

The live eval calls Amazon Bedrock (credentials and AWS_REGION required) but never calls openFDA:
tool outputs come from the case fixtures, so only the model varies. Compare several models in one run
to choose between them with data.

Options: --only H01,T07 limits to some cases; --out DIR sets where reports are written
(default evals/results). Exit code is 1 if the offline eval finds any invariant violation.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

from .harness import (CASES_PATH, load_cases, render_live, render_offline, run_live, run_offline, score_live)

DEFAULT_OUT = Path(__file__).parent / "results"


def _select(cases, only):
    if not only:
        return cases
    wanted = {x.strip() for x in only.split(",") if x.strip()}
    unknown = wanted - {c.id for c in cases}
    if unknown:
        raise SystemExit(f"unknown case id(s): {sorted(unknown)}")
    return [c for c in cases if c.id in wanted]


def cmd_offline(args) -> int:
    cases = _select(load_cases(Path(args.cases)), args.only)
    report = run_offline(cases)
    text = render_offline(report)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "offline-latest.md").write_text(text)
    (out / "offline-latest.json").write_text(json.dumps(report, indent=1))
    print(text)
    return 1 if report["violations"] else 0


def cmd_live(args) -> int:
    from recall_agent.agent import make_client  # imported here so the offline eval needs no boto3
    cases = _select(load_cases(Path(args.cases)), args.only)
    from recall_agent.guard import Guardrail
    guardrail = Guardrail(args.guardrail_id, args.guardrail_version) if args.guardrail_id else None
    client = make_client()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    day = datetime.date.today().isoformat()
    rows = []
    for model_id in args.model_id:
        print(f"== {model_id}: {len(cases)} cases x {args.repeats} repeats", file=sys.stderr)
        runs = run_live(client, model_id, cases, args.repeats, guardrail=guardrail,
                        progress=lambda cid, rep, final: print(f"   {cid} #{rep}: {final}", file=sys.stderr))
        score = score_live(runs, cases)
        slug = re.sub(r"[^a-z0-9]+", "-", model_id.lower()).strip("-")
        (out / f"live-{slug}-{day}.json").write_text(json.dumps({"model_id": model_id, "runs": runs, "score": score}, indent=1))
        text = render_live(model_id, score, args.repeats)
        (out / f"live-{slug}-{day}.md").write_text(text)
        print(text)
        rows.append((model_id, score))
    if len(rows) > 1:
        lines = ["# Live model comparison", "", "| Model | Accuracy (95% CI) | False CLEAR | False HOLD | Unsafe proposals caught | Over-escalated | Avg tokens in/out | Avg latency |",
                 "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for model_id, sc in rows:
            m, (lo, hi) = sc["metrics"], sc["accuracy_ci"]
            lines.append(f"| `{model_id}` | {sc['accuracy']:.0%} ({lo:.0%} to {hi:.0%}) | {m.get('false_clear', 0)} | {m.get('false_hold', 0)} | "
                         f"{m.get('raw_unsafe_caught', 0)}/{m.get('raw_unsafe', 0)} | {m.get('over_escalated', 0)} | "
                         f"{sc['avg_input_tokens']:.0f} / {sc['avg_output_tokens']:.0f} | {sc['avg_latency_s']:.1f}s |")
        text = "\n".join(lines) + "\n"
        (out / f"live-comparison-{day}.md").write_text(text)
        print(text)
    return 1 if any(sc["metrics"].get("false_clear", 0) or sc["metrics"].get("false_hold", 0) or sc["metrics"].get("crash", 0)
                    for _, sc in rows) else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.run", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("offline", "live"):
        sp = sub.add_parser(name)
        sp.add_argument("--cases", default=str(CASES_PATH))
        sp.add_argument("--only", default="")
        sp.add_argument("--out", default=str(DEFAULT_OUT))
    live = sub.choices["live"]
    live.add_argument("--model-id", action="append", required=True, help="repeat to compare models")
    live.add_argument("--repeats", type=int, default=3)
    live.add_argument("--guardrail-id", default="", help="also screen recall text with this Bedrock guardrail and report how it behaves")
    live.add_argument("--guardrail-version", default="DRAFT")
    args = ap.parse_args(argv)
    return cmd_offline(args) if args.cmd == "offline" else cmd_live(args)


if __name__ == "__main__":
    sys.exit(main())
