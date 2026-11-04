"""Compare real SearXNG/Ollama evidence on synthetic cases; never dispatch an LLM.

Default invocation only prints the plan. --live uses at most seven search HTTP requests:
one Ollama smoke plus two providers for each of three cases. Baseline reuses the same
SearXNG outcome, so the comparison does not spend a second set of requests.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import sys
import time

import httpx
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1] if "__file__" in globals() and __file__ != "<stdin>" else Path.cwd()
sys.path.insert(0, str(ROOT))

from backend.app.geography import find_place
from backend.app.models import TravelIntent
from backend.app.providers import Providers, city_mentions, lodging_result_matches, scoped_searches
from backend.app.retrieval import lodging_query
from backend.app.search.domain import fair_merge, url_identity
from backend.app.storage import Store


CASES = (
    {"id": "warsaw-larnaca-access", "origin": "Warsaw", "destination": "Larnaca",
     "pets": ["dog"], "hotel_requirements": ["step-free room", "roll-in shower"]},
    {"id": "berlin-prague-quiet", "origin": "Berlin", "destination": "Prague",
     "hotel_requirements": ["quiet room without street noise"]},
    {"id": "larnaca-athens-arrival", "origin": "Larnaca", "destination": "Athens",
     "hotel_requirements": ["late check-in", "luggage storage"]},
)


def serialize(hit):
    return asdict(hit)


def eligible_lodging(outcome, place, origin):
    accepted, rejected = [], []
    for hit in outcome.hits:
        relevant = lodging_result_matches(hit.row(), place, origin)
        relevant = relevant and not re.search(r"\bflights?\b|\u0430\u0432\u0438\u0430\u0431\u0438\u043b\u0435\u0442|\u043f\u0435\u0440\u0435\u043b[\u0435\u0451]\u0442", hit.title, re.I)
        (accepted if relevant else rejected).append(hit)
    return accepted, rejected


async def evaluate_case(case, providers):
    intent = TravelIntent(**{key: value for key, value in case.items() if key != "id"},
                          date_from=date(2026, 10, 31), date_to=date(2026, 11, 1), adults=1)
    place = find_place(case["destination"])
    query = dict(scoped_searches(place, intent))["lodging"]
    started = time.monotonic()
    outcomes = await providers.web_search.search(query)
    search_ms = round((time.monotonic() - started) * 1000)
    groups, provider_rows = [], []
    for outcome in outcomes:
        accepted, rejected = eligible_lodging(outcome, place, intent.origin)
        groups.append(accepted)
        provider_rows.append({"provider": outcome.provider, "status": outcome.status, "reason": outcome.reason,
                              "raw_hits": len(outcome.hits), "eligible_hits": len(accepted),
                              "hits": [serialize(hit) for hit in outcome.hits],
                              "rejected": [serialize(hit) for hit in rejected]})
    baseline = fair_merge(groups[:1])
    merged = fair_merge(groups)
    all_unique = {url_identity(hit.url) for group in groups for hit in group}
    ranking_started = time.monotonic()
    baseline_rows = await providers.ranker.rank_rows([hit.row() for hit in baseline], lodging_query(intent))
    merged_rows = await providers.ranker.rank_rows([hit.row() for hit in merged], lodging_query(intent))
    origin_place = find_place(intent.origin)
    selected = [row["_search_hit"] for row in merged_rows[:3]]
    return {"id": case["id"], "scope": "lodging", "origin": case["origin"], "destination": case["destination"],
            "query": query, "search_elapsed_ms": search_ms,
            "ranking_elapsed_ms": round((time.monotonic() - ranking_started) * 1000),
            "providers": provider_rows, "baseline_unique_eligible": len(baseline),
            "federated_unique_eligible": len(all_unique), "federated_ranker_candidates": len(merged),
            "deduplicated_eligible": sum(len(group) for group in groups) - len(all_unique),
            "baseline_display": [serialize(row["_search_hit"]) for row in baseline_rows[:3]],
            "federated_display": [serialize(hit) for hit in selected],
            "federated_display_provider_contributions": {name: sum(hit.provider == name for hit in selected)
                                                         for name in ("searxng", "ollama")},
            "selected_origin_city_mentions": sum(city_mentions(hit.title + " " + hit.url, origin_place) for hit in selected),
            "stopped_on_rate_limit": any(row.reason == "rate_limited" or row.status == "cooldown" for row in outcomes)}


async def benchmark(count):
    load_dotenv(ROOT / ".env")
    store = Store(":memory:")
    result = {"started_at": datetime.now(timezone.utc).isoformat(), "synthetic_requests": True,
              "live_sources": True, "llm_calls": 0, "max_search_http_requests": 1 + 2 * count,
              "comparison": "baseline reuses the SearXNG outcome of each same-query federated collection",
              "cases": []}
    try:
        async with httpx.AsyncClient(follow_redirects=False, headers={"User-Agent": "OneRequest-LocalPoC/0.1 (synthetic search validation; no booking)"}) as client:
            providers = Providers(client, store)
            result["capabilities_before"] = providers.search_capabilities()
            ollama = providers.web_search.adapters[1]
            started = time.monotonic()
            smoke = await ollama.search("Ollama official web search documentation")
            result["smoke"] = {"provider": smoke.provider, "status": smoke.status, "reason": smoke.reason,
                               "hits": len(smoke.hits), "elapsed_ms": round((time.monotonic() - started) * 1000),
                               "sample_urls": [hit.url for hit in smoke.hits[:3]]}
            if smoke.status in ("disabled", "unavailable", "cooldown"):
                result["stopped_reason"] = smoke.reason or smoke.status
            else:
                for case in CASES[:count]:
                    row = await evaluate_case(case, providers)
                    result["cases"].append(row)
                    if row["stopped_on_rate_limit"]:
                        result["stopped_reason"] = "rate_limited"
                        break
            result["capabilities_after"] = providers.search_capabilities()
            metrics = store.metrics()
            result["search_http_requests"] = {name: metrics.get("search_" + name + "_requests", 0)
                                              for name in ("searxng", "ollama")}
            result["embedding_metrics"] = {key: value for key, value in metrics.items() if key.startswith("embedding_")}
    finally:
        store.close()
    result["completed_at"] = datetime.now(timezone.utc).isoformat()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly spend hosted search requests; never generation")
    parser.add_argument("--cases", type=int, default=3, choices=(1, 2, 3))
    parser.add_argument("--output", type=Path, help="Optional sanitized JSON output; omitted means stdout")
    args = parser.parse_args()
    if not args.live:
        print(json.dumps({"live": False, "max_search_http_requests": 1 + 2 * args.cases,
                          "cases": [case["id"] for case in CASES[:args.cases]], "llm_calls": 0}))
        return
    result = asyncio.run(benchmark(args.cases))
    encoded = json.dumps(result, ensure_ascii=True, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
        print(json.dumps({"output": str(args.output), "cases": len(result["cases"]),
                          "requests": result.get("search_http_requests"), "stopped_reason": result.get("stopped_reason")}))
    else:
        print(encoded)


if __name__ == "__main__":
    main()
