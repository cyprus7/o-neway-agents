"""Model-free, non-persistent live QA through the running private data gateway.

Run inside the own agent container, where PROVIDER_GATEWAY_URL is configured.
This calls the gateway's actual bounded adapters and shared caches; it never
constructs a second provider client, opens a session or invokes inference.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Also allow direct execution from a source checkout; the deployed image uses
# stdin execution with /app already on its import path.
if Path(__file__).name != "<stdin>":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from fastapi import FastAPI

from backend.app.geography import find_place
from backend.app.infrastructure.http_travel_data import HttpTravelDataPort
from backend.app.models import TravelIntent
from one_request_telemetry import create_runtime, instrument_app, span


def source_url(value):
    if not value:
        return value
    parsed = urlsplit(value)
    if parsed.username or parsed.password:
        return None
    query = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
             if not re.search(r"key|token|secret|password|authorization|signature", key, re.I)]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def component_summary(value):
    quote = value.quote
    return {"title": value.title, "source_url": source_url(value.source_url),
            "pricing_entity_id": value.details.get("pricing_entity_id"),
            "pricing_delivery": value.details.get("pricing_delivery"),
            "pricing_outcomes": [{key: source_url(val) if key == "source_url" else val for key, val in row.items()}
                                 for row in value.details.get("pricing_outcomes", [])],
            "refresh_warning": value.details.get("pricing_refresh_warning"),
            "flight_route": value.details.get("flight_route"),
            "quote": {key: source_url(val) if key == "source_url" else val for key, val in
                      quote.model_dump(mode="json", exclude={"request_context_hash", "source_observation_ids"}).items()}
                     if quote is not None else None}


async def validate(args):
    gateway_url = os.getenv("PROVIDER_GATEWAY_URL", "").strip()
    if not gateway_url:
        raise ValueError("Run in the own agent container with PROVIDER_GATEWAY_URL configured")
    place, origin = find_place("Berlin"), find_place("Larnaca")
    if place is None or origin is None:
        raise ValueError("Reviewed geography unavailable")
    intent = TravelIntent(origin=origin.name, destination=place.name, date_from=args.date_from,
                          date_to=args.date_to, adults=args.adults, rooms=1, currency="EUR",
                          direct_flight_preferred=args.prefer_direct,
                          max_travel_minutes=args.max_travel_minutes,
                          hard_constraints=["direct_flight"] if args.direct_required else [],
                          hotel_requirements=args.hotel_wish)
    exported = []

    def accepted(spans):
        for item in spans:
            exported.append({"trace_id": f"{item.context.trace_id:032x}", "span_id": f"{item.context.span_id:016x}",
                             "parent_span_id": f"{item.parent.span_id:016x}" if item.parent else None,
                             "name": item.name})

    app = FastAPI()

    @app.get("/synthetic/live-data")
    async def run():
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            port = HttpTravelDataPort(client, gateway_url)
            before_agent = (await client.get("http://127.0.0.1:8080/api/metrics", timeout=5)).json()
            before_gateway = (await client.get(gateway_url.rstrip("/") + "/metrics", timeout=5)).json()
            with span("synthetic.validation", attributes={"operation": "search"}):
                evidence = await port.search(place, intent)
            with span("synthetic.validation", attributes={"operation": "inventory"}):
                initial = await port.inventory(place, origin, intent)
            with span("synthetic.validation", attributes={"operation": "refresh_inventory"}):
                refreshed = await port.refresh_inventory(place, origin, intent, initial)
            after_gateway = (await client.get(gateway_url.rstrip("/") + "/metrics", timeout=5)).json()
            after_agent = (await client.get("http://127.0.0.1:8080/api/metrics", timeout=5)).json()
        return {"kind": "live structured-intent QA; no session or model invocation", "scope": {
                    "origin": "LCA", "destination": "BER", "date_from": str(args.date_from),
                    "date_to": str(args.date_to), "adults": args.adults, "rooms": 1, "currency": "EUR",
                    "prefer_direct": args.prefer_direct, "direct_required": args.direct_required,
                    "max_travel_minutes": args.max_travel_minutes,
                    "hotel_requirements": args.hotel_wish},
                "search_evidence": [{"provider": row.provider, "label": row.label,
                                     "source_url": source_url(row.source_url), "retrieved_at": row.retrieved_at}
                                    for row in evidence],
                "initial": {name: component_summary(getattr(initial, name)) for name in ("flight", "stay", "transfer")},
                "refreshed": {name: component_summary(getattr(refreshed, name)) for name in ("flight", "stay", "transfer")},
                "model_requests_delta": after_agent.get("llm_requests", 0) - before_agent.get("llm_requests", 0),
                "gateway_metric_delta": {key: value - before_gateway.get(key, 0) for key, value in after_gateway.items()
                                         if isinstance(value, (int, float)) and value != before_gateway.get(key, 0)}}

    instrument_app(app, "one-request-validation", runtime_factory=lambda settings: create_runtime(settings, on_export=accepted))
    started = datetime.now(timezone.utc).isoformat()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as client:
            result = (await client.get("/synthetic/live-data")).json()
    return {"started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
            **result, "acknowledged_validation_spans": exported,
            "receiver_readback_required": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", type=date.fromisoformat, default=date(2026, 10, 30))
    parser.add_argument("--date-to", type=date.fromisoformat, default=date(2026, 11, 1))
    parser.add_argument("--hotel-wish", action="append", default=[])
    parser.add_argument("--adults", type=int, choices=(1, 2), default=2)
    parser.add_argument("--prefer-direct", action="store_true")
    parser.add_argument("--direct-required", action="store_true")
    parser.add_argument("--max-travel-minutes", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = asyncio.run(validate(args))
    encoded = json.dumps(result, ensure_ascii=True, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)
    return 0 if result["model_requests_delta"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
