"""Bounded public-source smoke for feature007; default is a network-free plan.

--live performs at most four official GETs, without search, credentials, inference,
the running app, or its database. Two supported sources use the production fetcher.
The Athens/Tbilisi reads are explicitly research diagnostics on unsupported hosts;
they cannot become production event facts. No redirect or failed request is retried.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.geography import find_place
from backend.app.regional_events.adapters import FetchedDocument, OfficialEventFetcher
from backend.app.regional_events.domain import (
    CACHE_TTL, MAX_HTML_BYTES, SourceFact, eligible_event, parse_source_events, source_url,
)
from backend.app.storage import Store


@dataclass(frozen=True)
class Case:
    id: str
    destination: str
    date_from: str
    date_to: str
    url: str
    production_supported: bool


CASES = (
    Case("istanbul-regatta", "Istanbul", "2026-10-31", "2026-11-01",
         "https://goturkiye.com/events/7th-presidential-international-yacht-races-republic-cup", True),
    Case("prague-concert", "Prague", "2026-10-27", "2026-10-28",
         "https://www.ceskafilharmonie.cz/en/event/35655-czech-chamber-music-society-jeremias-pestalozzi/", True),
    Case("athens-unsupported-gap", "Athens", "2026-10-31", "2026-11-01",
         "https://www.thisisathens.org/events/anyma-presents-aeden-in-athens", False),
    Case("tbilisi-unsupported-gap", "Tbilisi", "2026-11-08", "2026-11-14",
         "https://opera.ge/eng/page/Operacrown/", False),
)


class RequestBudget:
    """Enforce a fixed URL allowlist and global limit before transmission."""

    def __init__(self, cases: tuple[Case, ...]):
        self.limit = len(cases)
        self.allowed = {(urlsplit(case.url).hostname, urlsplit(case.url).path, "") for case in cases}
        self.requests: list[dict] = []

    async def before_request(self, request: httpx.Request) -> None:
        host = request.headers.get("host", "")
        target = (host, request.url.path, request.url.query.decode("ascii"))
        if request.method != "GET" or target not in self.allowed:
            raise ValueError("benchmark_fixed_url_only")
        if len(self.requests) >= self.limit:
            raise ValueError("benchmark_http_budget_exhausted")
        request.extensions["benchmark_index"] = len(self.requests)
        self.requests.append({"method": "GET", "host": host, "path": request.url.path,
                              "status": None, "content_type": None})

    async def after_response(self, response: httpx.Response) -> None:
        row = self.requests[response.request.extensions["benchmark_index"]]
        row["status"] = response.status_code
        row["content_type"] = response.headers.get("content-type", "").split(";", 1)[0]


class StructuredDataCount(HTMLParser):
    """Count LD containers for diagnostics; never extract an event from page text."""

    def __init__(self) -> None:
        super().__init__()
        self.blocks = 0

    def handle_starttag(self, tag, attrs):
        content_type = (dict(attrs).get("type") or "").split(";", 1)[0].strip().casefold()
        if tag == "script" and content_type == "application/ld+json":
            self.blocks += 1


async def fetch_unsupported_diagnostic(client: httpx.AsyncClient, case: Case) -> FetchedDocument | None:
    """One fixed research GET, no redirects, bounded body/time; not a production adapter."""
    try:
        async with asyncio.timeout(8):
            async with client.stream("GET", case.url, follow_redirects=False, timeout=7,
                                     headers={"Accept": "text/html,application/xhtml+xml"}) as response:
                if response.status_code != 200:
                    return None
                mime = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                if mime not in ("text/html", "application/xhtml+xml"):
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(body) + len(chunk) > MAX_HTML_BYTES:
                        return None
                    body.extend(chunk)
                return FetchedDocument(case.url, body.decode(response.encoding or "utf-8", errors="replace"),
                                       datetime.now(timezone.utc).isoformat())
    except (TimeoutError, OSError, httpx.HTTPError, ValueError, UnicodeError):
        return None


def cache_and_scope_checks(store: Store, case: Case, facts: list[SourceFact]) -> dict:
    """Exercise actual SQLite cache without changing source observation timestamps."""
    place = find_place(case.destination)
    wrong_city = find_place("Warsaw")
    start, end = date.fromisoformat(case.date_from), date.fromisoformat(case.date_to)
    key = "regional-event-benchmark:" + case.id
    store.cache_put(key, [fact.model_dump(mode="json") for fact in facts], CACHE_TTL)
    entry = store.cache_get(key)
    cached = [SourceFact.model_validate(row) for row in entry[0]] if entry else []
    cached = [fact.model_copy(update={"source": fact.source.model_copy(update={"freshness": "cached"})})
              for fact in cached]
    live_events = [event for fact in facts if (event := eligible_event(fact, place, start, end))]
    cached_events = [event for fact in cached if (event := eligible_event(fact, place, start, end))]
    return {
        "scope_checks_from_actual_facts": bool(facts),
        "cache_roundtrip": bool(facts) and entry is not None and not entry[2],
        "original_retrieved_at_preserved": bool(facts) and
            [fact.source.retrieved_at for fact in facts] == [fact.source.retrieved_at for fact in cached],
        "cached_identity_unchanged": bool(live_events) and
            [event.id for event in live_events] == [event.id for event in cached_events],
        "cached_freshness_visible": bool(cached_events) and
            all(event.source.freshness == "cached" for event in cached_events),
        "wrong_city_rejected": bool(facts) and
            all(eligible_event(fact, wrong_city, start, end) is None for fact in facts),
        "nonoverlapping_dates_rejected": bool(facts) and
            all(eligible_event(fact, place, end + timedelta(days=30), end + timedelta(days=31)) is None
                for fact in facts),
        "wrong_year_rejected": bool(facts) and
            all(eligible_event(fact, place, start.replace(year=start.year + 1), end.replace(year=end.year + 1)) is None
                for fact in facts),
    }


async def live_run(cases: tuple[Case, ...]) -> dict:
    budget = RequestBudget(cases)
    store = Store(":memory:")
    results = []
    try:
        # No implicit environment proxy/credential settings; production fetcher pins DNS.
        async with httpx.AsyncClient(trust_env=False, event_hooks={"request": [budget.before_request],
                                    "response": [budget.after_response]}) as client:
            fetcher = OfficialEventFetcher(client)
            for case in cases:
                started = time.monotonic()
                before = len(budget.requests)
                document = (await fetcher.fetch(case.url) if case.production_supported
                            else await fetch_unsupported_diagnostic(client, case))
                row = {**asdict(case), "fetch_mode": "production_pinned_fetcher" if case.production_supported
                       else "research_only_unsupported_host", "elapsed_ms": round((time.monotonic() - started) * 1000),
                       "http_requests": len(budget.requests) - before, "fetch_succeeded": document is not None,
                       "production_url_accepted": source_url(case.url) is not None,
                       "source_facts": [], "eligible_events": [], "checks": {}}
                if document is not None:
                    diagnostic = StructuredDataCount()
                    diagnostic.feed(document.html)
                    facts = parse_source_events(document.html, document.url, document.retrieved_at)
                    place = find_place(case.destination)
                    events = [event for fact in facts if (event := eligible_event(
                        fact, place, date.fromisoformat(case.date_from), date.fromisoformat(case.date_to)))]
                    row.update({"retrieved_at": document.retrieved_at, "html_utf8_bytes": len(document.html.encode("utf-8")),
                                "jsonld_blocks": diagnostic.blocks,
                                "source_facts": [fact.model_dump(mode="json") for fact in facts],
                                "eligible_events": [event.model_dump(mode="json") for event in events]})
                    if case.production_supported:
                        row["checks"] = cache_and_scope_checks(store, case, facts)
                        row["expectation_passed"] = bool(events) and all(row["checks"].values())
                    else:
                        row["checks"] = {"unsupported_host_cannot_create_production_facts": not facts and not events}
                        row["expectation_passed"] = all(row["checks"].values())
                else:
                    row["expectation_passed"] = False
                    row["limitation"] = "No accepted document; network/status/type/size/security/deadline may reject it."
                results.append(row)
        metrics = store.metrics()
        return {"mode": "live", "recorded_at": datetime.now(timezone.utc).isoformat(),
                "http_limit": budget.limit, "http_requests": len(budget.requests), "request_log": budget.requests,
                "llm_calls": 0, "search_calls": 0, "embedding_calls": 0,
                "storage": "isolated_sqlite_memory", "sessions_created": metrics["sessions"],
                "expectations_passed": all(row["expectation_passed"] for row in results), "cases": results,
                "limitations": [
                    "Fixed-page fetch/parser smoke; not federated discovery, app/SSE/UI, or full destination coverage.",
                    "Athens/Tbilisi diagnostic reads remain unsupported; empty facts do not mean no events exist.",
                    "Source-reported calendar dates only; tickets, attendance, prices and transport disruption are unverified.",
                    "A repeated live run performs new official GETs; no retries occur within this run.",
                ]}
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Run at most four fixed official GETs (no LLM/search).")
    parser.add_argument("--cases", type=int, choices=range(1, 5), default=4, help="Run first N fixed cases; default4.")
    parser.add_argument("--output", type=Path, help="Optional JSON report file; no page bodies or secrets are saved.")
    args = parser.parse_args()
    cases = CASES[:args.cases]
    report = asyncio.run(live_run(cases)) if args.live else {
        "mode": "dry_run", "http_requests": 0, "http_limit_if_live": len(cases),
        "llm_calls": 0, "search_calls": 0, "embedding_calls": 0,
        "cases": [asdict(case) for case in cases],
        "note": "No network or database opened. Add --live to fetch the fixed official pages once.",
    }
    serialized = json.dumps(report, ensure_ascii=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 1 if args.live and not report["expectations_passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
