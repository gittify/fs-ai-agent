# Live model eval: `us.anthropic.claude-haiku-4-5-20251001-v1:0`

5 runs (5 cases x 1 repeats). Tool outputs are emulated, so only the model varies.

| Measure | Result |
| --- | --- |
| Final decision matches label | **5/5** (100%, 95% CI 57% to 100%) |
| False CLEAR in the final record | **0** (of which the recall was never surfaced by the model's searches: 0) |
| False HOLD in the final record | **0** |
| Model proposed an unsafe CLEAR/HOLD | 0, caught by guardrails: 0 |
| Over-escalated to review (label was HOLD or CLEAR) | 0 |
| Code guardrails blocked a correct proposal | 0 |
| Model ended without a decision | 2 |
| Crashes | 0 |
| **Bedrock Guardrails**: interventions / on the adversarial case missed / on legitimate cases (false alarms) / check errors | 2 / 1 / 0 / 0 |
| Avg model calls / input / output tokens per item | 0.6 / 804 / 228 |
| Avg latency per item | 2.5s |

| Category | Correct |
| --- | --- |
| adversarial | 3/3 |
| hold | 2/2 |

Confusion (rows = label, columns = final decision):

| Label | HOLD | CLEAR | NEEDS_REVIEW | NOT_CHECKED |
| --- | --- | --- | --- | --- |
| HOLD | 3 | 0 | 0 | 0 |
| CLEAR | 0 | 0 | 0 | 0 |
| NEEDS_REVIEW | 0 | 0 | 2 | 0 |
| NOT_CHECKED | 0 | 0 | 0 | 0 |

Labels were written by the builder from the policy in `evals/cases.json` and have not been adjudicated by a coordinator. A small, hand-built case set gives wide uncertainty: read the interval, not just the percentage.
