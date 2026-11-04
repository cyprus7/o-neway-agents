# Frozen agent acceptance corpus v1

`tuning.v1.json` contains six public tuning cases; `held-out.v1.json` contains
24 separately frozen evaluation cases. Do not tune on held-out inputs. Any future
input/outcome change needs a new corpus version and retains this baseline.
`manifest.v1.json` pins exact corpus bytes. `baseline.fixture.v1.json` is observed
fixture evidence, not a model/provider quality score.

There are exactly 30 cases, 10 multi-turn and eight adversarial/degraded cases
(overlap allowed). Stage `purchase` means simulation only. Each case declares its
useful outcome, unknown policy, retained/invalidated fields, scope/provenance and
zero-tolerance constraints. Exact assistant wording is never an assertion.

The historical baseline adapter covers clarification only: predetermined extraction patches
are passed through the real application planner, HTTP message/job/read routes and
SQLite. It tests application retention, not natural-language understanding. Its
scripted blocking question intentionally prevents provider discovery. Model
completion, app telemetry instrumentation and outbound HTTP are blocked. Future
capabilities remain explicitly pending, including their safety assertions.

The current fixture runner additionally exercises reviewed production adapters; see
`docs/agent-quality.md` and `current.fixture.v1.json` (30 passed, zero pending).
Historical baseline evidence stays unchanged. Inputs and expected outcomes remain frozen.

Run `.venv/Scripts/python.exe scripts/evaluate_agent.py --output tmp/agent-fixture.json`.
The optional `--live --base-url http://localhost:8080` is reserved for the later
QA window: it creates synthetic sessions and consumes shared app inference quota.
It has no direct model/key probe and sends messages sequentially. Live mode currently
executes only the 12 clarification cases; other cases remain pending there until a
reviewed live adapter is added. Fixture mode runs the current adapter set. Live field checks are exact; paraphrased semantic wishes
need expert review. A command acknowledgement never counts as completed planning.

Pending cases are included in denominator 30, not in passes. The optional 27/30
live target and the historical pricing002 gate are separate. No recommendation
latency is observed by the clarification adapter. `null` live dispatch/tool counts
mean unavailable, not zero. Elapsed wall times vary; semantic fixture results are
deterministic. Runner exit 1 means an observed assertion/operation failed; pending
cases remain visible and prevent any claim of complete acceptance.
