# Agent quality checks

The repository includes a frozen scenario corpus, scripted fixture adapters and regression
tests. Fixture results establish behavior under controlled inputs; they do not establish
live model quality, expert semantic review, current inventory or provider coverage.

Inputs and checksum policy are in [the corpus manifest](../tests/agent_scenarios/manifest.v1.json),
with [tuning](../tests/agent_scenarios/tuning.v1.json) and
[held-out](../tests/agent_scenarios/held-out.v1.json) scenarios. The manifest uses
`utf8-lf-no-bom-v1`: strict UTF-8, no BOM, normalize CRLF to LF, reject lone CR and
preserve all other bytes. This keeps scenario identity stable across checkout platforms.

Run the fixture evaluator to a new local report:

```text
python scripts/evaluate_agent.py --output tmp/agent-quality.fixture.json
python -m pytest backend/tests/test_agent_corpus.py tests/test_host_productization.py -q
python scripts/evaluate_wishes.py
```

The evaluator covers clarification, scoped alternatives, comparison/explanation, refinement,
stale checkout rejection, fictional receipts, owner isolation, consented memory, deletion,
Telegram duplicate handling, provider failures, interruption/restart and mounted hosts.
Aftercare scenarios exercise unsent drafts, fixed-property amendments and consented reminders.
Read each report's adapter and per-case observations before interpreting its pass count.

Scripted model calls and intercepted Telegram requests are fixture activity. Latency from
these adapters is fixture wall time. Null measurements mean unmeasured, not zero. Source
records do not prove operating businesses; illustrative totals do not prove affordability.

Live evaluation is separately opt-in through the running application and shared limiter.
It consumes quota and creates synthetic sessions. An accepted job is not a completed result;
check terminal status and the resulting plan. Credentials, cookie state and raw provider
pages must not enter reports. Keep live observations separate from fixture reports.
