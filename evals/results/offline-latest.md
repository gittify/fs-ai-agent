# Offline guardrail eval

29 labeled cases x 11 model behaviors = 319 runs through the real agent loop, matcher and guardrails. The openFDA API is emulated; the model is scripted.

| Invariant | What it means | Violations / applicable runs |
| --- | --- | --- |
| SAFE-1 | never a false CLEAR (anything that is not labeled CLEAR must not end CLEAR) | **0** / 275 |
| SAFE-2 | never a false HOLD (only labeled HOLD cases may end HOLD) | **0** / 209 |
| SAFE-3 | an unverifiable item with no evidence is always NOT_CHECKED | **0** / 66 |
| SAFE-4 | a found recall (confirmed or possible) is never dropped or hidden by an error | **0** / 209 |
| SAFE-5 | evidence cited in the record was actually returned by the tool | **0** / 319 |
| COVER-1 | with a model that searches nothing, the code's standard queries alone surface every recall that should be held or reviewed | **0** / 19 |
| LIVE-1 | a correct model proposal is never blocked by the guardrails | **0** / 23 |
| CRASH | no model behavior may crash the agent | **0** / 319 |

**Total violations: 0.**
