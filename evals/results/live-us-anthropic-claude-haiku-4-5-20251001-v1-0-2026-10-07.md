# Live model eval: `us.anthropic.claude-haiku-4-5-20251001-v1:0`

29 runs (29 cases x 1 repeats). Tool outputs are emulated, so only the model varies.

| Measure | Result |
| --- | --- |
| Final decision matches label | **28/29** (97%, 95% CI 83% to 99%) |
| False CLEAR in the final record | **0** (of which the recall was never surfaced by the model's searches: 0) |
| False HOLD in the final record | **0** |
| Model proposed an unsafe CLEAR/HOLD | 4, caught by guardrails: 4 |
| Over-escalated to review (label was HOLD or CLEAR) | 1 |
| Code guardrails blocked a correct proposal | 0 |
| Model ended without a decision | 1 |
| Crashes | 0 |
| **Bedrock Guardrails**: interventions / on the adversarial case missed / on legitimate cases (false alarms) / check errors | 1 / 0 / 0 / 0 |
| Avg model calls / input / output tokens per item | 1.2 / 1703 / 463 |
| Avg latency per item | 4.7s |

| Category | Correct |
| --- | --- |
| adversarial | 1/1 |
| clear | 4/4 |
| hold | 7/8 |
| review | 8/8 |
| tool_failure | 8/8 |

Confusion (rows = label, columns = final decision):

| Label | HOLD | CLEAR | NEEDS_REVIEW | NOT_CHECKED |
| --- | --- | --- | --- | --- |
| HOLD | 8 | 0 | 1 | 0 |
| CLEAR | 0 | 4 | 0 | 0 |
| NEEDS_REVIEW | 0 | 0 | 10 | 0 |
| NOT_CHECKED | 0 | 0 | 0 | 6 |

Labels were written by the builder from the policy in `evals/cases.json` and have not been adjudicated by a coordinator. A small, hand-built case set gives wide uncertainty: read the interval, not just the percentage.
